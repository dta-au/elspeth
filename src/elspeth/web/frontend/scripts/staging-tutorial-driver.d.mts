import type { Page } from "@playwright/test";

export const FREEFORM_BUILD_ACTION_NAMES: string[];

export function isComposeRequest(url: string, method: string): boolean;
export function isRunRequest(url: string, method: string): boolean;
export function isAuditRequest(url: string, method: string): boolean;

type DriverRole = Parameters<Page["getByRole"]>[0];
interface DriverRoleOptions { name?: string | RegExp; exact?: boolean }
/** The Locator members the Build driver uses; a Playwright Locator satisfies it. */
export interface DriverLocator {
  waitFor(options?: { state?: "attached" | "detached" | "visible" | "hidden"; timeout?: number }): Promise<void>;
  click(options?: { timeout?: number }): Promise<void>;
  isEnabled(): Promise<boolean>;
  isVisible(): Promise<boolean>;
  textContent(): Promise<string | null>;
  count(): Promise<number>;
  nth(index: number): DriverLocator;
  first(): DriverLocator;
  evaluate(pageFunction: (element: HTMLElement) => void): Promise<void>;
  getByRole(role: DriverRole, options?: DriverRoleOptions): DriverLocator;
}
/** The Page members the Build driver uses; a Playwright Page satisfies it. */
export interface DriverPage {
  getByRole(role: DriverRole, options?: DriverRoleOptions): DriverLocator;
  locator(selector: string): DriverLocator;
  waitForTimeout(timeout: number): Promise<void>;
}

export function resolveVisibleReviews(page: DriverPage): Promise<number>;

export interface FreeformTutorialOptions {
  timeoutMs?: number;
}

export function driveFreeformTutorial(page: DriverPage, options?: FreeformTutorialOptions): Promise<void>;

export interface GraduationPreferences {
  tutorial_completed_at: string | null;
  tutorial_stage: string | null;
  tutorial_session_id: string | null;
  tutorial_run_id: string | null;
  tutorial_source_data_hash: string | null;
}

export function finishTutorialAndVerifyGraduation(
  page: Page,
  sessionId: string | null,
  loadPreferences: () => Promise<GraduationPreferences>,
): Promise<{ completed_at: string; landed_session_id: string }>;
