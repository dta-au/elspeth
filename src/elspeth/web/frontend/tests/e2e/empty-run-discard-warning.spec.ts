import { expect, test } from "@playwright/test";

import {
  authedContext,
  createSession,
  deleteSession,
  tokenFromStorageState,
} from "./helpers/api";
import { ComposerPage } from "./page-objects/composer-page";

test.describe("empty run discard visibility", () => {
  test("run summary surfaces source-validation discards for an empty fixture run", async ({
    page,
  }) => {
    const storageState = await page.context().storageState();
    const token = tokenFromStorageState(storageState);
    const ctx = await authedContext(token);

    try {
      const session = await createSession(ctx, "discard-warning-fixture");
      try {
        const runId = "run-discard-all-source-validation";
        // rows_rejected reconciles with discard_summary.validation_errors —
        // the backend now rejects the contradictory shape this fixture used
        // to hard-code (rows_processed: 0 with no row-unit rejection count).
        const runFixture = {
          id: runId,
          session_id: session.id,
          status: "empty",
          accounting: {
            source: { rows_processed: 0, rows_rejected: 2, rows_read: 2 },
            tokens: {
              emitted: 0,
              terminal: 0,
              succeeded: 0,
              failed: 0,
              structural: 0,
              pending: 0,
            },
            routing: {
              routed_success: 0,
              routed_failure: 0,
              quarantined: 0,
              discarded: 0,
            },
            integrity: {
              closure: "closed",
              missing_terminal_outcomes: 0,
              duplicate_terminal_outcomes: 0,
            },
          },
          error: null,
          started_at: "2026-05-24T08:00:00.000Z",
          finished_at: "2026-05-24T08:00:01.000Z",
          composition_version: 1,
          discard_summary: {
            total: 2,
            validation_errors: 2,
            transform_errors: 0,
            gate_errors: 0,
            sink_discards: 0,
            stages: [
              {
                stage: "source_validation",
                node_id: "source_csv_upload",
                count: 2,
              },
            ],
          },
        };

        await page.route(`**/api/sessions/${session.id}/runs`, async (route) => {
          await route.fulfill({ json: [runFixture] });
        });
        await page.route(`**/api/runs/${runId}/outputs`, async (route) => {
          await route.fulfill({
            json: {
              run_id: runId,
              landscape_run_id: "landscape-discard-fixture",
              artifacts: [],
            },
          });
        });
        // The banner fetches diagnostics to show the RECORDED rejection
        // reason for source-validation discards — the one discard class with
        // no token trail (elspeth-43f52d69a4).
        await page.route(`**/api/runs/${runId}/diagnostics*`, async (route) => {
          await route.fulfill({
            json: {
              run_id: runId,
              landscape_run_id: "landscape-discard-fixture",
              run_status: "empty",
              cancel_requested: false,
              summary: {
                token_count: 0,
                preview_limit: 50,
                preview_truncated: false,
                discard_count: 2,
                state_counts: {},
                operation_counts: { source_load: 1 },
                latest_activity_at: "2026-05-24T08:00:01.000Z",
              },
              tokens: [],
              operations: [],
              artifacts: [],
              discards: [
                {
                  stage: "source_validation",
                  node_id: "source_csv_upload",
                  schema_mode: "fixed",
                  error:
                    "1 validation error: amount: Input should be a valid integer, unable to parse string as an integer [int_parsing]",
                  created_at: "2026-05-24T08:00:00.500Z",
                },
                {
                  stage: "source_validation",
                  node_id: "source_csv_upload",
                  schema_mode: "fixed",
                  error:
                    "1 validation error: amount: Input should be a valid integer, unable to parse string as an integer [int_parsing]",
                  created_at: "2026-05-24T08:00:00.600Z",
                },
              ],
              failure_detail: null,
            },
          });
        });

        const composer = new ComposerPage(page);
        await composer.goto(session.id);
        await composer.waitForChatReady();

        const warning = page.getByRole("alert").filter({
          hasText: /2 rows discarded at source validation/i,
        });
        await expect(warning).toBeVisible();
        await expect(warning).toContainText("source_csv_upload");
        await expect(warning).toContainText("Run terminated empty");
        // The RECORDED reason (already boundary-scrubbed server-side), not
        // the old generic "Common causes" guess with its false "view
        // diagnostics" pointer.
        await expect(warning).toContainText("Recorded rejection reason");
        await expect(warning).toContainText("int_parsing");
      } finally {
        await deleteSession(ctx, session.id);
      }
    } finally {
      await ctx.dispose();
    }
  });
});
