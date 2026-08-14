// Composer navigation and modal flow spec — persistent Graph/YAML artifacts,
// explicit Graph focus mode, and Catalog drawer.
//
// Graph/YAML shortcuts and deep links select the persistent workspace tabs.
// The Graph modal remains available only through the explicit Focus Graph
// action; Catalog remains a modal drawer.
//
// Existing modal-adjacent stubs (yaml-export-roundtrip.spec.ts,
// topology.spec.ts) are both tracked test.skip (elspeth-7cf763da7c) — they
// still need seeded spec implementations and test a different surface (YAML
// round-trip correctness and topology validation parity). Neither exercises the
// modal open/close or keyboard affordances this spec covers; there is no overlap.
//
// Pattern: REST session creation via helpers/api.ts (same as smoke.spec.ts),
// then UI interaction. No LLM calls.

import { join } from "node:path";

import { expect, test, type APIRequestContext } from "@playwright/test";

import {
  authedContext,
  createSession,
  deleteSession,
  seedCompositionState,
  tokenFromStorageState,
  uploadBlob,
} from "./helpers/api";
import { ComposerPage } from "./page-objects/composer-page";

const SEEDED_SOURCE_FILENAME = "modal-flow-input.csv";
const SEEDED_SOURCE_CONTENT = "id\n1\n";

function storagePathForUploadedBlob(sessionId: string, blobId: string): string {
  const dataDir = process.env.PLAYWRIGHT_E2E_DATA_DIR;
  if (!dataDir) {
    throw new Error("PLAYWRIGHT_E2E_DATA_DIR is required for seeded blob-backed state");
  }
  return join(dataDir, "blobs", sessionId, `${blobId}_${SEEDED_SOURCE_FILENAME}`);
}

function exportableCompositionState(sessionId: string, blobId: string) {
  return {
    version: 1,
    metadata: { name: "E2E exportable pipeline", description: "" },
    sources: {
      source: {
        plugin: "csv",
        on_success: "results",
        options: {
          path: storagePathForUploadedBlob(sessionId, blobId),
          blob_ref: blobId,
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
        options: { path: "outputs/output.csv", schema: { mode: "observed" } },
        on_write_failure: "discard",
      },
    ],
  };
}

async function seedExportableCompositionState(
  ctx: APIRequestContext,
  sessionId: string,
): Promise<void> {
  const blob = await uploadBlob(
    ctx,
    sessionId,
    SEEDED_SOURCE_FILENAME,
    SEEDED_SOURCE_CONTENT,
  );
  await seedCompositionState(ctx, sessionId, exportableCompositionState(sessionId, blob.id));
}

// ── Shared afterEach cleanup ──────────────────────────────────────────────────
// Each test creates its own session; cleanup is out-of-band so a failing test
// does not accumulate orphaned sessions. The deleteSession helper tolerates
// 404 (session already gone).

test.describe("modal flows — Graph, YAML, Catalog", () => {
  // ── Graph modal ──────────────────────────────────────────────────────────

  test.describe("Graph modal", () => {
    test("explicit Focus Graph action opens the Graph modal; Escape closes it", async ({
      page,
    }) => {
      const storageState = await page.context().storageState();
      const token = tokenFromStorageState(storageState);
      const ctx = await authedContext(token);
      let sessionId: string | undefined;
      try {
        const session = await createSession(ctx, "pw-3b-graph-open-close");
        sessionId = session.id;
        const composer = new ComposerPage(page);
        await composer.goto(sessionId);
        await composer.waitForChatReady();

        await page.getByRole("button", { name: "Focus graph" }).click();

        const dialog = page.getByRole("dialog", { name: /pipeline graph/i });
        await expect(dialog).toBeVisible();

        await page.keyboard.press("Escape");
        await expect(dialog).not.toBeVisible();
      } finally {
        if (sessionId !== undefined) {
          await deleteSession(ctx, sessionId);
        }
        await ctx.dispose();
      }
    });

    test("deep link /#/{id}/graph selects Graph and rewrites hash to canonical", async ({
      page,
    }) => {
      await page.setViewportSize({ width: 720, height: 720 });
      const storageState = await page.context().storageState();
      const token = tokenFromStorageState(storageState);
      const ctx = await authedContext(token);
      let sessionId: string | undefined;
      try {
        const session = await createSession(ctx, "pw-3b-graph-deeplink");
        sessionId = session.id;

        await page.goto(`/#/${sessionId}/graph`);
        await page.getByTestId("composer-workspace").waitFor();

        const graphTab = page.getByRole("tab", { name: "Graph" });
        await expect(page.getByRole("tab", { name: "Pipeline", exact: true })).toHaveAttribute("aria-selected", "true");
        await expect(graphTab).toHaveAttribute("aria-selected", "true");
        await expect(graphTab).toBeFocused();
        await expect(
          page.getByRole("dialog", { name: /pipeline graph/i }),
        ).toHaveCount(0);

        // Hash must be rewritten to canonical (verb fragment stripped).
        await expect(page).toHaveURL(new RegExp(`#/${sessionId}$`));
      } finally {
        if (sessionId !== undefined) {
          await deleteSession(ctx, sessionId);
        }
        await ctx.dispose();
      }
    });

    test("Ctrl+Shift+G keyboard shortcut selects the Graph artifact", async ({
      page,
    }) => {
      await page.setViewportSize({ width: 720, height: 720 });
      const storageState = await page.context().storageState();
      const token = tokenFromStorageState(storageState);
      const ctx = await authedContext(token);
      let sessionId: string | undefined;
      try {
        const session = await createSession(ctx, "pw-3b-graph-shortcut");
        sessionId = session.id;
        const composer = new ComposerPage(page);
        await composer.goto(sessionId);
        await composer.waitForChatReady();

        await page.getByRole("tab", { name: "Pipeline", exact: true }).click();
        await page.getByRole("tab", { name: "Run" }).click();
        await expect(page.getByRole("tab", { name: "Run" })).toHaveAttribute(
          "aria-selected",
          "true",
        );
        await page.getByRole("tab", { name: "Compose", exact: true }).click();

        await page.keyboard.press("Control+Shift+G");

        const graphTab = page.getByRole("tab", { name: "Graph" });
        await expect(page.getByRole("tab", { name: "Pipeline", exact: true })).toHaveAttribute("aria-selected", "true");
        await expect(graphTab).toHaveAttribute("aria-selected", "true");
        await expect(graphTab).toBeFocused();
        await expect(
          page.getByRole("dialog", { name: /pipeline graph/i }),
        ).toHaveCount(0);
      } finally {
        if (sessionId !== undefined) {
          await deleteSession(ctx, sessionId);
        }
        await ctx.dispose();
      }
    });
  });

  // ── YAML artifact ────────────────────────────────────────────────────────

  test.describe("YAML artifact", () => {
    test("Export YAML command selects and focuses the persistent YAML artifact", async ({
      page,
    }) => {
      await page.setViewportSize({ width: 720, height: 720 });
      const storageState = await page.context().storageState();
      const token = tokenFromStorageState(storageState);
      const ctx = await authedContext(token);
      let sessionId: string | undefined;
      try {
        const session = await createSession(ctx, "pw-3b-yaml-open-close");
        sessionId = session.id;
        await seedExportableCompositionState(ctx, sessionId);
        const composer = new ComposerPage(page);
        await composer.goto(sessionId);
        await composer.waitForChatReady();

        // Export YAML is content-gated. Seed an exportable state above so the
        // command is available before exercising its narrow-view transition.
        await page.getByRole("tab", { name: "Pipeline", exact: true }).click();
        const exportYamlBtn = page.getByRole("button", {
          name: /export yaml/i,
        });
        await expect(exportYamlBtn).toBeVisible();
        await expect(exportYamlBtn).toBeEnabled();
        await page.getByRole("tab", { name: "Compose", exact: true }).click();
        await page.keyboard.press("Control+k");
        await page.getByRole("option", { name: "Export YAML" }).click();

        const yamlTab = page.getByRole("tab", { name: "YAML" });
        await expect(page.getByRole("tab", { name: "Pipeline", exact: true })).toHaveAttribute("aria-selected", "true");
        await expect(yamlTab).toHaveAttribute("aria-selected", "true");
        await expect(yamlTab).toBeFocused();
        await expect(page.getByRole("tabpanel", { name: "YAML" })).toBeVisible();
        await expect(
          page.getByRole("dialog", { name: /export yaml/i }),
        ).toHaveCount(0);
      } finally {
        if (sessionId !== undefined) {
          await deleteSession(ctx, sessionId);
        }
        await ctx.dispose();
      }
    });

    test("deep link /#/{id}/yaml selects YAML and rewrites hash to canonical", async ({
      page,
    }) => {
      await page.setViewportSize({ width: 720, height: 720 });
      const storageState = await page.context().storageState();
      const token = tokenFromStorageState(storageState);
      const ctx = await authedContext(token);
      let sessionId: string | undefined;
      try {
        const session = await createSession(ctx, "pw-3b-yaml-deeplink");
        sessionId = session.id;
        await seedExportableCompositionState(ctx, sessionId);

        await page.goto(`/#/${sessionId}/yaml`);
        await page.getByTestId("composer-workspace").waitFor();

        const yamlTab = page.getByRole("tab", { name: "YAML" });
        await expect(page.getByRole("tab", { name: "Pipeline", exact: true })).toHaveAttribute("aria-selected", "true");
        await expect(yamlTab).toHaveAttribute("aria-selected", "true");
        await expect(yamlTab).toBeFocused();
        await expect(
          page.getByRole("dialog", { name: /export yaml/i }),
        ).toHaveCount(0);

        await expect(page).toHaveURL(new RegExp(`#/${sessionId}$`));
      } finally {
        if (sessionId !== undefined) {
          await deleteSession(ctx, sessionId);
        }
        await ctx.dispose();
      }
    });

    test("Ctrl+Shift+Y keyboard shortcut selects the YAML artifact", async ({
      page,
    }) => {
      await page.setViewportSize({ width: 720, height: 720 });
      const storageState = await page.context().storageState();
      const token = tokenFromStorageState(storageState);
      const ctx = await authedContext(token);
      let sessionId: string | undefined;
      try {
        const session = await createSession(ctx, "pw-3b-yaml-shortcut");
        sessionId = session.id;
        await seedExportableCompositionState(ctx, sessionId);
        const composer = new ComposerPage(page);
        await composer.goto(sessionId);
        await composer.waitForChatReady();

        // State hydration is asynchronous. The shortcut uses the same
        // composition-content gate as this button, so wait for that gate.
        await page.getByRole("tab", { name: "Pipeline", exact: true }).click();
        await expect(page.getByRole("button", { name: /export yaml/i })).toBeEnabled();
        await page.getByRole("tab", { name: "Compose", exact: true }).click();
        await page.keyboard.press("Control+Shift+Y");

        const yamlTab = page.getByRole("tab", { name: "YAML" });
        await expect(page.getByRole("tab", { name: "Pipeline", exact: true })).toHaveAttribute("aria-selected", "true");
        await expect(yamlTab).toHaveAttribute("aria-selected", "true");
        await expect(yamlTab).toBeFocused();
        await expect(
          page.getByRole("dialog", { name: /export yaml/i }),
        ).toHaveCount(0);
      } finally {
        if (sessionId !== undefined) {
          await deleteSession(ctx, sessionId);
        }
        await ctx.dispose();
      }
    });
  });

  test.describe("narrow persistent artifact deep links", () => {
    for (const [verb, tab] of [["spec", "Spec"], ["runs", "Run"]] as const) {
      test(`/${verb} reveals Pipeline and focuses ${tab}`, async ({ page }) => {
        await page.setViewportSize({ width: 720, height: 720 });
        const storageState = await page.context().storageState();
        const token = tokenFromStorageState(storageState);
        const ctx = await authedContext(token);
        let sessionId: string | undefined;
        try {
          const session = await createSession(ctx, `pw-3b-${verb}-deeplink`);
          sessionId = session.id;
          if (verb === "spec") await seedExportableCompositionState(ctx, sessionId);

          await page.goto(`/#/${sessionId}/${verb}`);
          await page.getByTestId("composer-workspace").waitFor();

          await expect(page.getByRole("tab", { name: "Pipeline", exact: true })).toHaveAttribute("aria-selected", "true");
          const artifactTab = page.getByRole("tab", { name: tab, exact: true });
          await expect(artifactTab).toHaveAttribute("aria-selected", "true");
          await expect(artifactTab).toBeFocused();
          await expect(page).toHaveURL(new RegExp(`#/${sessionId}$`));
        } finally {
          if (sessionId !== undefined) await deleteSession(ctx, sessionId);
          await ctx.dispose();
        }
      });
    }
  });

  // ── Catalog modal ─────────────────────────────────────────────────────────
  // CatalogDrawer renders as a drawer (role="dialog" with name "Plugin Catalog")
  // opened by the OPEN_CATALOG_EVENT. No hash-routed deep link exists for
  // catalog (useHashRouter owns only persistent artifact verbs).
  // Deep-link assertion is skipped per the task brief.

  test.describe("Catalog modal", () => {
    test("Catalog (reference) button opens the Catalog drawer; Escape closes it", async ({
      page,
    }) => {
      const storageState = await page.context().storageState();
      const token = tokenFromStorageState(storageState);
      const ctx = await authedContext(token);
      let sessionId: string | undefined;
      try {
        const session = await createSession(ctx, "pw-3b-catalog-open-close");
        sessionId = session.id;
        const composer = new ComposerPage(page);
        await composer.goto(sessionId);
        await composer.waitForChatReady();

        await page.getByRole("button", { name: "More actions" }).click();
        const catalogBtn = page.getByRole("button", {
          name: /catalog \(reference\)/i,
        });
        await expect(catalogBtn).toBeVisible();
        await catalogBtn.click();

        const drawer = page.getByRole("dialog", { name: /plugin catalog/i });
        await expect(drawer).toBeVisible();

        await page.keyboard.press("Escape");
        await expect(drawer).not.toBeVisible();
      } finally {
        if (sessionId !== undefined) {
          await deleteSession(ctx, sessionId);
        }
        await ctx.dispose();
      }
    });

    test("Ctrl+Shift+P keyboard shortcut opens the Catalog drawer", async ({
      page,
    }) => {
      const storageState = await page.context().storageState();
      const token = tokenFromStorageState(storageState);
      const ctx = await authedContext(token);
      let sessionId: string | undefined;
      try {
        const session = await createSession(ctx, "pw-3b-catalog-shortcut");
        sessionId = session.id;
        const composer = new ComposerPage(page);
        await composer.goto(sessionId);
        await composer.waitForChatReady();

        await page.keyboard.press("Control+Shift+P");

        await expect(
          page.getByRole("dialog", { name: /plugin catalog/i }),
        ).toBeVisible();
      } finally {
        if (sessionId !== undefined) {
          await deleteSession(ctx, sessionId);
        }
        await ctx.dispose();
      }
    });
  });
});
