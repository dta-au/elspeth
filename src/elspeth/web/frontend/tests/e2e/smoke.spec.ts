// Smoke spec — end-to-end proof of life for the Playwright harness.
//
// Verifies the full boot path:
//   1. globalSetup ran and wrote storageState with a valid auth_token
//      (otherwise the app redirects to /login and these tests fail).
//   2. Both webServer instances came up healthy on the Playwright-assigned
//      backend and frontend ports.
//   3. The frontend SPA loads, restores auth from localStorage, completes
//      session bootstrap, and renders the stable application shell.
//   4. The backend's /api/sessions endpoint accepts the bearer token and
//      can create + delete a session.
//
// What this spec deliberately does NOT do:
//   - Drive the LLM compose loop (would cost money / be nondeterministic).
//   - Mutate composition state (seeded composer-correctness specs own that).
//   - Assert on visual layout or styling.
//
// The richer composer-correctness specs that target epic elspeth-e1ab67e55a
// live alongside this file as tracked test.skip() stubs (see ./topology.spec.ts
// etc., tracked as elspeth-7cf763da7c). The through-UI compose happy path is
// tracked separately as elspeth-617e1ca703 because it still needs an LLM stub.

import { expect, test } from "@playwright/test";

import {
  authedContext,
  createSession,
  deleteSession,
  tokenFromStorageState,
} from "./helpers/api";
import { ComposerPage } from "./page-objects/composer-page";

test.describe("smoke — boot + auth + session shell", () => {
  test("frontend completes bootstrap and renders the session shell", async ({
    page,
  }) => {
    const composer = new ComposerPage(page);
    const sessionsLoaded = page.waitForResponse((response) => {
      const url = new URL(response.url());
      return (
        url.pathname === "/api/sessions" &&
        url.searchParams.get("include_archived") === "true" &&
        response.request().method() === "GET" &&
        response.status() === 200
      );
    });

    await composer.goto();
    await sessionsLoaded;

    // These shell elements are stable whether bootstrap finds no sessions or
    // automatically resumes an existing session.
    await expect(
      page.getByRole("heading", { name: "ELSPETH Pipeline Composer" }),
    ).toBeAttached();
    await expect(
      page.getByRole("button", { name: /session switcher:/i }),
    ).toBeVisible();
  });

  test("backend accepts authed token and round-trips a session", async ({
    page,
  }) => {
    // Read the same storageState the browser context loaded, then issue
    // an out-of-band REST request to confirm the bearer token works.
    const storageState = await page.context().storageState();
    const token = tokenFromStorageState(storageState);

    const ctx = await authedContext(token);
    try {
      const session = await createSession(ctx, "playwright-smoke");
      expect(session.id).toMatch(/^[a-zA-Z0-9_-]+$/);
      expect(session.title).toBe("playwright-smoke");
      await deleteSession(ctx, session.id);
    } finally {
      await ctx.dispose();
    }
  });

  test("composer URL with a session id navigates without error", async ({
    page,
  }) => {
    // Seed a session via API, then navigate the SPA to its hash route.
    // This proves the hash router resolves a session id without depending
    // on the header session-switcher UI.
    const storageState = await page.context().storageState();
    const token = tokenFromStorageState(storageState);

    const ctx = await authedContext(token);
    try {
      const session = await createSession(ctx, "playwright-smoke-hash");
      const composer = new ComposerPage(page);
      await composer.goto(session.id);
      await composer.waitForChatReady();
      // The chat panel renders when an active session is present —
      // (when no session, the empty-state copy from test 1 shows instead).
      await expect(page.getByLabel("Chat panel")).toBeVisible();
      await deleteSession(ctx, session.id);
    } finally {
      await ctx.dispose();
    }
  });
});
