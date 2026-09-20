// Seed an existing test session through the production guided-start API.
// Mode selection is covered by composer-preferences.spec.ts; wizard journey
// tests keep their uploaded fixtures on the same session.
import { randomUUID } from "node:crypto";
import { expect, type Page } from "@playwright/test";
import { authedContext, tokenFromStorageState } from "./api";

export async function startGuidedWithGoal(page: Page, goal: string): Promise<void> {
  const sessionId = new URL(page.url()).hash.slice(2);
  if (!sessionId || sessionId.includes("/")) {
    throw new Error("Expected an active session before starting the guided fixture");
  }
  const ctx = await authedContext(tokenFromStorageState(await page.context().storageState()));
  try {
    const response = await ctx.post(`/api/sessions/${sessionId}/guided/start`, {
      data: { profile: "live", intent: goal, operation_id: randomUUID() },
    });
    if (!response.ok()) {
      throw new Error(`Guided fixture start failed (${response.status()}): ${await response.text()}`);
    }
  } finally {
    await ctx.dispose();
  }
  await page.reload();
  await expect(page.getByLabel("Guided composer", { exact: true })).toBeVisible();
}
