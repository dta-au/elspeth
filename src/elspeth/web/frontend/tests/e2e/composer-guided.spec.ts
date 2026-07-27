// E2E spec: guided-mode wizard source/output walk against the live local
// backend. The local Playwright backend intentionally has no LLM provider, so
// this stops after output review, immediately before "Finish outputs" invokes
// the provider-dependent guided planner. Later-stage behavior is covered by
// tutorial.spec.ts with a deterministic guided protocol fixture.

import { expect, test, type Page } from "@playwright/test";

import {
  authedContext,
  createSession,
  deleteSession,
  tokenFromStorageState,
  uploadBlob,
} from "./helpers/api";
import { ComposerPage } from "./page-objects/composer-page";

const BLOB_FILENAME = "playwright-orders.csv";

// Minimal CSV content: header + one data row. The "category" column lets us
// satisfy the classify recipe's classifier-keyword required-field predicate.
const SAMPLE_CSV = "id,name,category\n1,widget,a\n";

// Sink paths are deployment-relative and resolve inside the managed outputs
// directory; absolute host paths are deliberately rejected.
const SINK_OUTPUT_PATH = "playwright-guided-output.jsonl";

async function isolateAuditReadinessSideRail(
  page: Page,
  sessionId: string,
): Promise<void> {
  await page.route(`**/api/sessions/${sessionId}/validate`, async (route) => {
    await route.fulfill({
      json: {
        is_valid: true,
        summary: "Guided demo pipeline validates.",
        checks: [],
        errors: [],
        warnings: [],
        semantic_contracts: [],
      },
    });
  });

  await page.route(`**/api/sessions/${sessionId}/audit-readiness`, async (route) => {
    await route.fulfill({
      json: {
        session_id: sessionId,
        composition_version: 1,
        checked_at: "2026-05-19T12:00:00Z",
        rows: [
          {
            id: "validation",
            label: "Validation",
            status: "ok",
            summary: "Guided demo pipeline validates.",
            detail: null,
            component_ids: [],
          },
          {
            id: "plugin_trust",
            label: "Plugin trust",
            status: "ok",
            summary: "Guided demo plugins are trusted.",
            detail: null,
            component_ids: [],
          },
          {
            id: "provenance",
            label: "Provenance",
            status: "not_applicable",
            summary: "No run provenance yet.",
            detail: null,
            component_ids: [],
          },
          {
            id: "retention",
            label: "Retention",
            status: "not_applicable",
            summary: "No run retention yet.",
            detail: null,
            component_ids: [],
          },
          {
            id: "llm_interpretations",
            label: "LLM interpretations",
            status: "not_applicable",
            summary: "No interpretation events.",
            detail: null,
            component_ids: [],
          },
          {
            id: "secrets",
            label: "Secrets",
            status: "not_applicable",
            summary: "No secret checks in the guided demo.",
            detail: null,
            component_ids: [],
          },
        ],
        validation_result: {
          is_valid: true,
          summary: "Guided demo pipeline validates.",
          checks: [],
          errors: [],
          warnings: [],
          semantic_contracts: [],
        },
      },
    });
  });
}

test.describe("composer-guided — source/output live walk", () => {
  test(
    "current decision settles at the bottom in the regular chat measure",
    async ({ page }) => {
      await page.setViewportSize({ width: 1600, height: 600 });

      const storageState = await page.context().storageState();
      const token = tokenFromStorageState(storageState);
      const ctx = await authedContext(token);

      let sessionId: string | undefined;
      try {
        const session = await createSession(ctx, "playwright-guided-current-decision");
        sessionId = session.id;

        const composer = new ComposerPage(page);
        await composer.goto(sessionId);
        await composer.waitForChatReady();
        await page.getByRole("button", { name: "Switch to guided" }).click();
        await expect(page.getByRole("button", { name: "CSV", exact: true })).toBeVisible();

        const geometry = await page
          .locator(".guided-workspace-scroll")
          .evaluate((scroll) => {
            const decision = scroll.querySelector(".guided-current-decision");
            if (!(decision instanceof HTMLElement)) {
              throw new Error("Current Decision panel is missing");
            }

            const scrollRect = scroll.getBoundingClientRect();
            const decisionRect = decision.getBoundingClientRect();
            const rootFontSize = Number.parseFloat(
              getComputedStyle(document.documentElement).fontSize,
            );
            return {
              remainingScroll:
                scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight,
              decisionWidth: decisionRect.width,
              regularChatWidth: 56 * rootFontSize,
              leftInset: decisionRect.left - scrollRect.left,
              rightInset: scrollRect.right - decisionRect.right,
            };
          });

        expect(geometry.remainingScroll).toBe(0);
        expect(geometry.decisionWidth).toBeLessThanOrEqual(
          geometry.regularChatWidth + 1,
        );
        expect(Math.abs(geometry.leftInset - geometry.rightInset)).toBeLessThanOrEqual(
          1,
        );
      } finally {
        if (sessionId !== undefined) {
          await deleteSession(ctx, sessionId);
        }
        await ctx.dispose();
      }
    },
  );

  test(
    "guided demo: CSV source → reviewed JSONL output",
    async ({ page }) => {
      // ── Out-of-band setup ──────────────────────────────────────────────────
      // Create session + upload CSV blob via REST before navigating the SPA.
      const storageState = await page.context().storageState();
      const token = tokenFromStorageState(storageState);
      const ctx = await authedContext(token);

      let sessionId: string | undefined;
      try {
        const session = await createSession(ctx, "playwright-guided-demo");
        sessionId = session.id;
        await isolateAuditReadinessSideRail(page, sessionId);

        const blob = await uploadBlob(ctx, sessionId, BLOB_FILENAME, SAMPLE_CSV);

        // ── Navigate + enter guided mode ─────────────────────────────────────
        // "Switch to guided" resolves to the live/empty profile via GET /guided.
        const composer = new ComposerPage(page);
        await composer.goto(sessionId);
        await composer.waitForChatReady();
        await page.getByRole("button", { name: "Switch to guided" }).click();
        await expect(page.getByLabel(/guided composer/i)).toBeVisible();

        // ── Step 1 source: SINGLE_SELECT — pick "csv" ──────────────────────
        await expect(
          page.getByRole("button", { name: "CSV", exact: true }),
        ).toBeVisible();
        await page.getByRole("button", { name: "CSV", exact: true }).click();

        // ── Step 1 source: SCHEMA_FORM — schema, path, on_validation_failure
        await page.getByRole("button", { name: "Edit", exact: true }).click();
        await expect(page.getByLabel(/^schema/i)).toBeVisible();
        await page.getByLabel(/^schema/i).fill('{"mode":"observed"}');
        // Uploaded sources are intentionally blob-bound and read-only in the
        // editor. Assert the binding instead of trying to overwrite its path.
        await expect(page.getByText(`blob:${blob.id}`, { exact: true })).toBeVisible();
        await page.getByLabel(/on\s+validation\s+failure/i).fill("discard");
        await expect(
          page.getByRole("button", { name: "Continue", exact: true }),
        ).toBeEnabled();
        await page.getByRole("button", { name: "Continue", exact: true }).click();

        // Uploaded tabular sources are inspected before they are committed.
        // Confirm the observed columns so the wizard can advance to output.
        await expect(
          page.getByRole("button", { name: "Looks right", exact: true }),
        ).toBeVisible();
        await page.getByRole("button", { name: "Looks right", exact: true }).click();
        await expect(
          page.getByRole("button", { name: "Finish sources", exact: true }),
        ).toBeEnabled();
        await page.getByRole("button", { name: "Finish sources", exact: true }).click();

        // ── Step 2 sink: SINGLE_SELECT — pick "json" ───────────────────────
        await expect(
          page.getByRole("button", { name: "JSON", exact: true }),
        ).toBeVisible();
        await page.getByRole("button", { name: "JSON", exact: true }).click();

        // ── Step 2 sink: SCHEMA_FORM — path + collision_policy + format + mode
        // The file sink requires `mode` set explicitly (write|append).
        await page.getByRole("button", { name: "Edit", exact: true }).click();
        await expect(page.getByLabel(/^schema/i)).toBeVisible();
        await page.getByLabel(/^schema/i).fill('{"mode":"observed"}');
        await page.getByLabel(/^path/i).fill(SINK_OUTPUT_PATH);
        await page.getByLabel(/collision.?policy/i).selectOption("auto_increment");
        await page.getByLabel(/^format$/i).selectOption("jsonl");
        await page.getByLabel(/^mode$/i).selectOption("write");
        await expect(
          page.getByRole("button", { name: "Continue", exact: true }),
        ).toBeEnabled();
        await page.getByRole("button", { name: "Continue", exact: true }).click();

        // ── Step 2 required fields: MULTI_SELECT_WITH_CUSTOM ──────────────
        // "category" is already selected by default, so the required-field
        // review can continue without adding a custom field.
        await expect(page.getByText("category")).toBeVisible();
        await page.getByRole("button", { name: "Continue", exact: true }).click();
        await expect(
          page.getByRole("button", { name: "Finish outputs", exact: true }),
        ).toBeEnabled();

        // "Finish outputs" is the planner handoff and therefore requires an
        // available provider. Verify the complete live source/output walk at
        // that boundary; tutorial.spec.ts owns the deterministic later stages.
        const outputReview = page.getByRole("region", { name: "Review outputs" });
        await expect(outputReview).toBeVisible();
        await expect(outputReview.getByText("output", { exact: true })).toBeVisible();
        await expect(outputReview.getByText("json", { exact: true })).toBeVisible();
        await expect(outputReview.getByText("reviewed", { exact: true })).toBeVisible();
        await expect(page.getByRole("textbox", { name: "Message input" })).toBeEnabled();
        await expect(
          page.getByRole("button", { name: "Exit to freeform", exact: true }),
        ).toBeVisible();
        await expect(
          page.getByText(/Source commit failed|Chat panel encountered an error/i),
        ).toHaveCount(0);
      } finally {
        if (sessionId !== undefined) {
          await deleteSession(ctx, sessionId);
        }
        await ctx.dispose();
      }
    },
  );
});
