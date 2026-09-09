// Entering guided mode from the freeform composer, goal-first (elspeth-378cfa0e18).
//
// "Switch to guided" no longer switches on the click. For a session that is not
// resuming a saved wizard, ModeSwitchButton always opens its confirm card and
// the card collects the session's GOAL: a guided session with no root intent
// cannot reach the planner (the step-2 finish refuses with
// `guided_planner_intent_required`), and the store cannot tell an empty session
// from a worked one synchronously, so the goal is asked for once and
// `enterGuided(goal)` routes it — start for an empty session, convert for a
// worked one.
//
// Every spec that enters guided this way goes through here, so the day the
// card's copy changes there is ONE place to follow it.

import { expect, type Page } from "@playwright/test";

/** The switch affordance in the freeform composer. */
export const SWITCH_TO_GUIDED = "Switch to guided";

/** The goal field's accessible name on the confirm card (a real <label>). */
export const GUIDED_GOAL_LABEL = "What should this pipeline produce?";

/** The card's confirming primary. */
export const CONFIRM_SWITCH_TO_GUIDED = "Confirm switch to guided";

/**
 * Click "Switch to guided", state *goal*, and confirm.
 *
 * The caller keeps its own post-conditions (the guided surface, the first
 * wizard turn): this helper only owns the card. It asserts the confirming
 * primary is enabled, so a goal the card rejects fails here with the card on
 * screen rather than later as a missing wizard.
 */
export async function switchToGuidedWithGoal(page: Page, goal: string): Promise<void> {
  await page.getByRole("button", { name: SWITCH_TO_GUIDED }).click();
  await page.getByLabel(GUIDED_GOAL_LABEL).fill(goal);
  const confirm = page.getByRole("button", { name: CONFIRM_SWITCH_TO_GUIDED, exact: true });
  await expect(confirm).toBeEnabled();
  await confirm.click();
}
