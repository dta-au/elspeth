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

// One attempt's actionability wait (visible, stable, enabled, receives events), never a response wait: 2 s lets a card's entry animation or a brief overlay clear on the loaded 4-vCPU staging box.
const ENABLED_CLICK_TIMEOUT_MS = 2_000;
// Attempts on a button that stays enabled while refusing: 3 gives a slow-settling button about 6 s in all, then Playwright's own error ends the walk.
const STILL_ENABLED_CLICK_ATTEMPTS = 3;

// Click the first enabled button `buttons` matches; the locator re-resolves when the click runs.
// After the click, Playwright waits only for a page navigation the click starts (these buttons
// start none), so the timeout never covers the request the click sends.
// A refused click goes uncounted, and the caller rescans, only when `buttons` matches no enabled
// button afterwards. While one is left, even on another card, the refusal counts in `refusals`
// (reset when a click lands) and the last attempt rethrows Playwright's error.
async function clickFirstEnabled(buttons, refusals) {
  try {
    await buttons.first().click({ timeout: ENABLED_CLICK_TIMEOUT_MS });
  } catch (error) {
    if (await buttons.count().catch(() => 0) === 0) return false;
    refusals.count += 1;
    if (refusals.count >= STILL_ENABLED_CLICK_ATTEMPTS) throw error;
    return false;
  }
  refusals.count = 0;
  return true;
}

export async function resolveVisibleReviews(page) {
  const promptRegions = page.getByRole("region", { name: "Prompt template review" });
  const refusals = { count: 0 };
  let actions = 0;
  reviewPass: for (let guard = 0; guard < 12; guard += 1) {
    // evaluateAll() acts on the regions present now and never waits; nth(i).evaluate() on a
    // region that unmounted after the count would wait for the default timeout.
    await promptRegions.evaluateAll((elements) => {
      for (const element of elements) {
        element.scrollTop = element.scrollHeight;
        element.dispatchEvent(new Event("scroll"));
      }
    }).catch(() => {});
    let clicked = false;
    for (const name of ACKNOWLEDGEMENT_ACTIONS) {
      // count() reads the page once; a waiting check such as isEnabled() on an absent
      // locator would block for the default timeout. The click re-resolves the first
      // enabled button when it runs, so no index crosses from the check to the action.
      const enabledButtons = page.getByRole("button", { name, disabled: false });
      if (await enabledButtons.count().catch(() => 0) === 0) continue;
      // A refused click rescans on the next pass.
      if (!(await clickFirstEnabled(enabledButtons, refusals))) continue reviewPass;
      actions += 1;
      clicked = true;
      break;
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

  // A deliberate walk total, never reset on the disabled branch: a reset would let a Continue to Run that keeps toggling run to the deadline again.
  const continueRefusals = { count: 0 };
  const acceptRefusals = { count: 0 };
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    // count() reads the page once; isEnabled() on these usually-absent buttons would wait
    // for the default timeout on every poll. Only enabled buttons match, and a click that
    // is refused because a new review disabled or removed the button rescans.
    const continueToRun = page.getByRole("button", { name: FREEFORM_BUILD_ACTION_NAMES[1], disabled: false });
    if (await continueToRun.count().catch(() => 0) > 0) {
      if (!(await clickFirstEnabled(continueToRun, continueRefusals))) continue;
      await page.getByRole("heading", { name: /Ready to run/i }).waitFor({ state: "visible", timeout: 60_000 });
      await page.getByRole("button", { name: FREEFORM_BUILD_ACTION_NAMES[2], exact: true }).click();
      await page.getByRole("button", { name: "Continue", exact: true })
        .waitFor({ state: "visible", timeout: timeoutMs });
      return;
    }
    const accept = page.getByRole("region", { name: /Awaiting your decision/ })
      .getByRole("button", { name: /Accept proposal:/, disabled: false });
    if (await accept.count().catch(() => 0) > 0) {
      await clickFirstEnabled(accept, acceptRefusals);
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
