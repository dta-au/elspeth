// Standalone browser driver for the ordinary freeform first-run tutorial.

export const FREEFORM_BUILD_ACTION_NAMES = ["Send tutorial brief", "Continue to Run", "Run"];

const ACKNOWLEDGEMENT_ACTIONS = [
  /^View prompt$/,
  /^Approve the LLM prompt template$/,
  /^Acknowledge/i,
];

export function isComposeRequest(url, method) {
  return method.toUpperCase() === "POST" &&
    /\/api\/sessions\/[0-9a-f-]{36}\/messages(?:[?#]|$)/i.test(url);
}

export function isRunRequest(url, method) {
  return method.toUpperCase() === "POST" && /\/api\/tutorial\/run(?:[?#]|$)/i.test(url);
}

export function isAuditRequest(url, method) {
  return method.toUpperCase() === "GET" &&
    /\/api\/sessions\/[0-9a-f-]{36}\/runs\/[^/]+\/audit-story(?:[?#]|$)/i.test(url);
}

async function isEnabled(locator) {
  return locator.isEnabled().catch(() => false);
}

export async function resolveVisibleReviews(page) {
  const promptRegions = page.getByRole("region", { name: "Prompt template review" });
  let actions = 0;
  for (let guard = 0; guard < 12; guard += 1) {
    const regionCount = await promptRegions.count().catch(() => 0);
    for (let i = 0; i < regionCount; i += 1) {
      await promptRegions.nth(i).evaluate((element) => {
        element.scrollTop = element.scrollHeight;
        element.dispatchEvent(new Event("scroll"));
      }).catch(() => {});
    }
    let clicked = false;
    for (const name of ACKNOWLEDGEMENT_ACTIONS) {
      const buttons = page.getByRole("button", { name });
      const count = await buttons.count().catch(() => 0);
      for (let i = 0; i < count; i += 1) {
        const button = buttons.nth(i);
        if (await isEnabled(button)) {
          await button.click();
          actions += 1;
          clicked = true;
          break;
        }
      }
      if (clicked) break;
    }
    if (!clicked) break;
  }
  return actions;
}

export async function driveFreeformTutorial(page, options = {}) {
  const timeoutMs = options.timeoutMs ?? 600_000;
  await page.getByRole("heading", { name: "Build with the Composer." })
    .waitFor({ state: "visible", timeout: 60_000 });

  const sendBrief = page.getByRole("button", { name: FREEFORM_BUILD_ACTION_NAMES[0] });
  await sendBrief.waitFor({ state: "visible", timeout: 30_000 });
  await sendBrief.click();

  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const continueToRun = page.getByRole("button", { name: FREEFORM_BUILD_ACTION_NAMES[1] });
    if (await isEnabled(continueToRun)) {
      await continueToRun.click();
      await page.getByRole("heading", { name: /Ready to run/i }).waitFor({ state: "visible", timeout: 60_000 });
      await page.getByRole("button", { name: FREEFORM_BUILD_ACTION_NAMES[2], exact: true }).click();
      await page.getByRole("button", { name: "Continue", exact: true })
        .waitFor({ state: "visible", timeout: timeoutMs });
      return;
    }
    const accept = page.getByRole("region", { name: /Awaiting your decision/ })
      .getByRole("button", { name: /Accept proposal:/ }).first();
    if (await isEnabled(accept)) {
      await accept.click();
      continue;
    }
    if (await resolveVisibleReviews(page) > 0) continue;
    const error = page.locator(".tutorial-error").first();
    if (await error.isVisible().catch(() => false)) {
      throw new Error(`freeform tutorial Build did not become runnable: ${await error.textContent()}`);
    }
    await page.waitForTimeout(1_000);
  }
  throw new Error("freeform tutorial Build never reached the Run turn before the deadline");
}

export async function finishTutorialAndVerifyGraduation(page, sessionId, loadPreferences) {
  if (typeof sessionId !== "string" || sessionId.length === 0) {
    throw new Error("tutorial graduation has no session id to verify");
  }
  await page.getByRole("heading", { name: "You're ready to use the composer." })
    .waitFor({ state: "visible", timeout: 60_000 });
  await page.getByRole("button", { name: "Take me to the composer" })
    .click({ timeout: 30_000 });
  await page.getByLabel("Chat panel", { exact: true })
    .waitFor({ state: "visible", timeout: 60_000 });
  await page.waitForURL((url) => url.hash === `#/${sessionId}`, { timeout: 60_000 });

  const preferences = await loadPreferences();
  if (
    typeof preferences?.tutorial_completed_at !== "string" ||
    preferences.tutorial_completed_at.length === 0 ||
    preferences.tutorial_stage !== null ||
    preferences.tutorial_session_id !== null ||
    preferences.tutorial_run_id !== null ||
    preferences.tutorial_source_data_hash !== null
  ) {
    throw new Error("tutorial graduation completion did not persist cleanly");
  }
  return {
    completed_at: preferences.tutorial_completed_at,
    landed_session_id: sessionId,
  };
}
