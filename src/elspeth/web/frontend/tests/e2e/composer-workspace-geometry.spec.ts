import { join } from "node:path";

import {
  expect,
  test,
  type APIRequestContext,
  type Locator,
} from "@playwright/test";

import {
  authedContext,
  createSession,
  deleteSession,
  seedCompositionState,
  tokenFromStorageState,
  uploadBlob,
} from "./helpers/api";

const SOURCE_FILENAME = "workspace-geometry.csv";

function sourcePath(sessionId: string, blobId: string): string {
  const dataDir = process.env.PLAYWRIGHT_E2E_DATA_DIR;
  if (!dataDir) {
    throw new Error("PLAYWRIGHT_E2E_DATA_DIR is required for workspace geometry");
  }
  return join(dataDir, "blobs", sessionId, `${blobId}_${SOURCE_FILENAME}`);
}

async function seedPopulatedComposition(
  ctx: APIRequestContext,
  sessionId: string,
): Promise<void> {
  const blob = await uploadBlob(ctx, sessionId, SOURCE_FILENAME, "id\n1\n");
  await seedCompositionState(ctx, sessionId, {
    version: 1,
    metadata: { name: "Workspace geometry", description: "" },
    sources: {
      source: {
        plugin: "csv",
        on_success: "results",
        options: {
          path: sourcePath(sessionId, blob.id),
          blob_ref: blob.id,
          schema: { mode: "observed" },
        },
        on_validation_failure: "discard",
      },
    },
    nodes: [],
    edges: [],
    outputs: [
      {
        name: "results",
        plugin: "csv",
        options: {
          path: "outputs/workspace-geometry.csv",
          schema: { mode: "observed" },
        },
        on_write_failure: "discard",
      },
    ],
  });
}

async function box(locator: Locator): Promise<{ width: number; height: number }> {
  const bounds = await locator.boundingBox();
  return {
    width: bounds?.width ?? 0,
    height: bounds?.height ?? 0,
  };
}

const TASK_5_VIEWPORTS = [
  { width: 1280, height: 720 },
  { width: 1536, height: 760 },
] as const;

test.describe("Composer workspace integration geometry", () => {
  for (const viewport of TASK_5_VIEWPORTS) {
    test(`persistent artifact is operable at ${viewport.width}x${viewport.height}`, async ({
      page,
    }) => {
      await page.setViewportSize(viewport);
      const token = tokenFromStorageState(await page.context().storageState());
      const ctx = await authedContext(token);
      let sessionId: string | undefined;
      try {
        const session = await createSession(
          ctx,
          `workspace ${viewport.width}x${viewport.height}`,
        );
        sessionId = session.id;
        await seedPopulatedComposition(ctx, sessionId);
        await page.goto(`/#/${sessionId}`);

        const authoring = page.getByRole("region", { name: "Authoring pane" });
        const artifact = page.getByRole("region", { name: "Pipeline artifact" });
        const activePanel = page.getByRole("tabpanel", { name: "Graph" });
        await expect(authoring).toBeVisible();
        await expect(artifact).toBeVisible();
        await expect(activePanel).toBeVisible();
        await expect
          .poll(async () => (await box(artifact)).width)
          .toBeGreaterThanOrEqual(640);
        await expect
          .poll(async () => (await box(activePanel)).height)
          .toBeGreaterThanOrEqual(420);

        const validation = page.getByRole("button", { name: /^Validation: / });
        const moreActions = page.getByRole("button", { name: "More actions" });
        await expect(validation).toBeVisible();
        await expect(moreActions).toBeVisible();
        for (const control of [validation, moreActions]) {
          const bounds = await control.boundingBox();
          expect(bounds).not.toBeNull();
          expect(bounds!.x).toBeGreaterThanOrEqual(0);
          expect(bounds!.y).toBeGreaterThanOrEqual(0);
          expect(bounds!.x + bounds!.width).toBeLessThanOrEqual(
            viewport.width,
          );
          expect(bounds!.y + bounds!.height).toBeLessThanOrEqual(
            viewport.height,
          );
        }

        const overflow = await page.evaluate(
          () =>
            document.documentElement.scrollWidth -
            document.documentElement.clientWidth,
        );
        expect(overflow).toBeLessThanOrEqual(0);
      } finally {
        if (sessionId !== undefined) await deleteSession(ctx, sessionId);
        await ctx.dispose();
      }
    });
  }
});
