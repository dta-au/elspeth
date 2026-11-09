// Shared graph import — the sharer shares a link that lets recipients import
// a copy; a second user opens it, imports, and lands in a new session of
// their own whose graph is attached to their own copies of both files.
//
// Sharer (e2e-tester, the default storageState): seeds a session through the
// API with an uploaded CSV source (blob_ref) and a reference_join transform
// whose reference table is a second uploaded file (an inline_content marker),
// then creates a 1-day import link through the real "Share inspect link"
// dialog. Recipient (e2e-recipient, its own browser context): opens
// /#/shared/<token>, clicks "Import a copy into my workspace", and must land at
// #/<new session id> with the import notice. The recipient's new session state
// is then read through the API: both attachments must name blobs of the
// recipient's session, never the sharer's.
//
// No LLM call: the graph has no LLM node, so validation and the completion
// gate pass with the composer provider unavailable (the E2E deployment).

import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";

import { expect, test, type APIRequestContext } from "@playwright/test";

import {
  authedContext,
  createSession,
  deleteSession,
  seedCompositionState,
  tokenFromStorageState,
  uploadBlob,
  type BlobMetadata,
  type CompositionStateSeed,
} from "./helpers/api";
import { ComposerPage } from "./page-objects/composer-page";

const HERE = dirname(fileURLToPath(import.meta.url));
// Written by setup/global-setup.ts beside the default user's state.
const RECIPIENT_STORAGE_STATE_PATH = resolve(HERE, ".auth", "recipient.json");

// Set by playwright.config.ts; absolute because a context created from the
// browser fixture is navigated explicitly rather than relying on baseURL.
const FRONTEND_BASE_URL = process.env.PLAYWRIGHT_FRONTEND_BASE_URL ?? "http://127.0.0.1:5173";

const PIPELINE_NAME = "Shared customer regions";
const CUSTOMERS_FILENAME = "shared-import-customers.csv";
const CUSTOMERS_CONTENT = "id,name\n1,Ada\n2,Grace\n";
const REGIONS_FILENAME = "shared-import-regions.csv";
const REGIONS_CONTENT = "id,region\n1,North\n2,South\n";

interface FullBlobMetadata extends BlobMetadata {
  session_id: string;
  content_hash: string | null;
}

interface InlineContentMarker {
  blob_ref: string;
  mode: string;
  sha256: string;
}

interface StateComponentOptions {
  options: Record<string, unknown>;
}

interface SessionStateResponse {
  sources: Record<string, StateComponentOptions>;
  nodes: (StateComponentOptions & { id: string; plugin: string })[];
}

function uploadedBlobPath(sessionId: string, blob: BlobMetadata): string {
  const dataDir = process.env.PLAYWRIGHT_E2E_DATA_DIR;
  if (!dataDir) {
    throw new Error("PLAYWRIGHT_E2E_DATA_DIR is required for seeded blob-backed state");
  }
  return join(dataDir, "blobs", sessionId, `${blob.id}_${blob.filename}`);
}

function sharedGraph(
  sessionId: string,
  customers: FullBlobMetadata,
  regions: FullBlobMetadata,
): CompositionStateSeed {
  if (regions.content_hash === null) {
    throw new Error("uploaded reference blob has no content_hash");
  }
  return {
    version: 1,
    metadata: { name: PIPELINE_NAME, description: "" },
    sources: {
      customers: {
        plugin: "csv",
        on_success: "lookup_in",
        on_validation_failure: "discard",
        options: {
          path: uploadedBlobPath(sessionId, customers),
          blob_ref: customers.id,
          schema: { mode: "observed" },
        },
      },
    },
    nodes: [
      {
        id: "lookup",
        node_type: "transform",
        plugin: "reference_join",
        input: "lookup_in",
        on_success: "results",
        on_error: "discard",
        options: {
          schema: { mode: "observed" },
          tables: {
            regions: {
              reference_format: "csv",
              reference_content: {
                blob_ref: regions.id,
                mode: "inline_content",
                sha256: regions.content_hash,
              },
            },
          },
          lookups: [
            {
              table: "regions",
              key_field: "id",
              reference_key_name: "id",
              output: { region: "ref['region']" },
            },
          ],
        },
      },
    ],
    edges: [],
    outputs: [
      {
        name: "results",
        plugin: "json",
        options: {
          path: "outputs/results.jsonl",
          format: "jsonl",
          schema: { mode: "observed" },
        },
        on_write_failure: "discard",
      },
    ],
  } as unknown as CompositionStateSeed;
}

async function listBlobs(ctx: APIRequestContext, sessionId: string): Promise<FullBlobMetadata[]> {
  const resp = await ctx.get(`/api/sessions/${sessionId}/blobs`);
  if (!resp.ok()) {
    throw new Error(
      `GET /api/sessions/${sessionId}/blobs failed (${resp.status()}): ${(await resp.text()).slice(0, 500)}`,
    );
  }
  return (await resp.json()) as FullBlobMetadata[];
}

async function readState(ctx: APIRequestContext, sessionId: string): Promise<SessionStateResponse> {
  const resp = await ctx.get(`/api/sessions/${sessionId}/state`);
  if (!resp.ok()) {
    throw new Error(
      `GET /api/sessions/${sessionId}/state failed (${resp.status()}): ${(await resp.text()).slice(0, 500)}`,
    );
  }
  return (await resp.json()) as SessionStateResponse;
}

test.describe("shared graph import", () => {
  test("a recipient imports a shared graph with both of its files", async ({ page, browser }) => {
    const sharerCtx = await authedContext(tokenFromStorageState(await page.context().storageState()));
    const recipientBrowserCtx = await browser.newContext({ storageState: RECIPIENT_STORAGE_STATE_PATH });
    const recipientCtx = await authedContext(
      tokenFromStorageState(await recipientBrowserCtx.storageState()),
    );
    let sharerSessionId: string | undefined;
    let recipientSessionId: string | undefined;
    try {
      // ── Sharer: seed a graph with two attached files ────────────────────
      const session = await createSession(sharerCtx, "pw-shared-graph-import");
      sharerSessionId = session.id;
      const customers = (await uploadBlob(
        sharerCtx,
        sharerSessionId,
        CUSTOMERS_FILENAME,
        CUSTOMERS_CONTENT,
      )) as FullBlobMetadata;
      const regions = (await uploadBlob(
        sharerCtx,
        sharerSessionId,
        REGIONS_FILENAME,
        REGIONS_CONTENT,
      )) as FullBlobMetadata;
      await seedCompositionState(
        sharerCtx,
        sharerSessionId,
        sharedGraph(sharerSessionId, customers, regions),
      );

      // ── Sharer: create a 1-day import link through the dialog ───────────
      const composer = new ComposerPage(page);
      await composer.goto(sharerSessionId);
      await composer.waitForChatReady();
      // Enabled once the auto-validation reports completion readiness.
      await expect(composer.saveForReview()).toBeEnabled({ timeout: 30_000 });
      await composer.saveForReview().click();

      const dialog = page.getByRole("dialog", { name: "Share for review" });
      await expect(dialog).toBeVisible();
      const importCheckbox = dialog.getByRole("checkbox", { name: "Let recipients import a copy" });
      await expect(importCheckbox).toBeEnabled({ timeout: 15_000 });
      await importCheckbox.check();
      const fileList = dialog.getByTestId("save-for-review-file-list");
      await expect(fileList).toContainText(CUSTOMERS_FILENAME);
      await expect(fileList).toContainText(REGIONS_FILENAME);
      const lifetimes = dialog.getByRole("group", { name: "Link expires after" });
      await lifetimes.getByRole("radio", { name: "1 day" }).check();
      await dialog.getByRole("button", { name: "Create link" }).click();

      const shareUrlInput = dialog.getByTestId("save-for-review-url-input");
      await expect(shareUrlInput).toBeVisible({ timeout: 15_000 });
      const shareUrl = await shareUrlInput.inputValue();
      const tokenMatch = /#\/shared\/([^/?#]+)$/.exec(shareUrl);
      expect(tokenMatch, `share URL ${shareUrl} carries a /#/shared/<token> fragment`).not.toBeNull();
      const shareToken = (tokenMatch as RegExpExecArray)[1];

      // The link the recipient opens is the 1-day, import-allowed one.
      const inspected = await recipientCtx.get(`/api/sessions/shared/${shareToken}`);
      expect(inspected.ok(), await inspected.text()).toBe(true);
      const inspectBody = (await inspected.json()) as { import_allowed: boolean; expires_at: string };
      expect(inspectBody.import_allowed).toBe(true);
      const lifetimeMs = Date.parse(inspectBody.expires_at) - Date.now();
      expect(lifetimeMs).toBeGreaterThan(23 * 60 * 60 * 1000);
      expect(lifetimeMs).toBeLessThanOrEqual(24 * 60 * 60 * 1000);

      // ── Recipient: open the link and import through the real UI ─────────
      const recipientPage = await recipientBrowserCtx.newPage();
      await recipientPage.goto(`${FRONTEND_BASE_URL}/#/shared/${shareToken}`);
      await expect(recipientPage.getByTestId("shared-inspect-loaded")).toBeVisible({ timeout: 15_000 });
      const importFiles = recipientPage.getByRole("list", { name: "Files that will be copied" });
      await expect(importFiles).toContainText(CUSTOMERS_FILENAME);
      await expect(importFiles).toContainText(REGIONS_FILENAME);
      await recipientPage.getByRole("button", { name: "Import a copy into my workspace" }).click();

      await expect(recipientPage).toHaveURL(/#\/[0-9a-f-]{36}$/, { timeout: 30_000 });
      const landed = /#\/([0-9a-f-]{36})$/.exec(recipientPage.url());
      recipientSessionId = (landed as RegExpExecArray)[1];
      expect(recipientSessionId).not.toBe(sharerSessionId);
      await expect(
        recipientPage.getByRole("button", { name: `Session switcher: Copy of ${PIPELINE_NAME}` }),
      ).toBeVisible({ timeout: 15_000 });
      // The notice center shows one notice and folds the rest behind "N more
      // notice(s)". With the composer unavailable here, the service notice
      // outranks the import notice, so open the list when it is folded.
      const importNotice = recipientPage.getByText("Imported from a link shared by e2e-tester.");
      const moreNotices = recipientPage.getByRole("button", { name: /more notices?$/ });
      if (!(await importNotice.isVisible()) && (await moreNotices.count()) > 0) {
        await moreNotices.click();
      }
      await expect(importNotice).toBeVisible();

      // ── Both files attached to the recipient's own copies ───────────────
      const sharerBlobIds = new Set([customers.id, regions.id]);
      const recipientBlobs = await listBlobs(recipientCtx, recipientSessionId);
      const recipientBlobsById = new Map(recipientBlobs.map((blob) => [blob.id, blob]));
      for (const blob of recipientBlobs) {
        expect(blob.session_id).toBe(recipientSessionId);
        expect(sharerBlobIds.has(blob.id)).toBe(false);
      }

      const state = await readState(recipientCtx, recipientSessionId);
      const sourceBlobRef = state.sources.customers?.options.blob_ref;
      expect(typeof sourceBlobRef).toBe("string");
      const sourceCopy = recipientBlobsById.get(sourceBlobRef as string);
      expect(sourceCopy, `source blob_ref ${String(sourceBlobRef)} is a recipient blob`).toBeDefined();
      expect(sourceCopy?.filename).toBe(CUSTOMERS_FILENAME);
      expect(sourceCopy?.content_hash).toBe(customers.content_hash);

      const lookup = state.nodes.find((node) => node.id === "lookup");
      expect(lookup?.plugin).toBe("reference_join");
      const tables = lookup?.options.tables as Record<string, { reference_content: InlineContentMarker }>;
      const marker = tables.regions.reference_content;
      expect(marker.mode).toBe("inline_content");
      expect(marker.sha256).toBe(regions.content_hash);
      const referenceCopy = recipientBlobsById.get(marker.blob_ref);
      expect(referenceCopy, `reference marker ${marker.blob_ref} is a recipient blob`).toBeDefined();
      expect(referenceCopy?.filename).toBe(REGIONS_FILENAME);
      expect(referenceCopy?.content_hash).toBe(regions.content_hash);

      expect(sharerBlobIds.has(sourceBlobRef as string)).toBe(false);
      expect(sharerBlobIds.has(marker.blob_ref)).toBe(false);
      expect(JSON.stringify(state)).not.toContain(customers.id);
      expect(JSON.stringify(state)).not.toContain(regions.id);
    } finally {
      if (recipientSessionId !== undefined) {
        await deleteSession(recipientCtx, recipientSessionId);
      }
      if (sharerSessionId !== undefined) {
        await deleteSession(sharerCtx, sharerSessionId);
      }
      await recipientCtx.dispose();
      await sharerCtx.dispose();
      await recipientBrowserCtx.close();
    }
  });
});
