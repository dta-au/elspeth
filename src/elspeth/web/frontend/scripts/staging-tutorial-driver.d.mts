import type { Page } from "@playwright/test";

export const FREEFORM_BUILD_ACTION_NAMES: string[];

export function isComposeRequest(url: string, method: string): boolean;
export function isRunRequest(url: string, method: string): boolean;
export function isAuditRequest(url: string, method: string): boolean;
export function resolveVisibleReviews(page: Page): Promise<number>;

export interface FreeformTutorialOptions {
  timeoutMs?: number;
}

export function driveFreeformTutorial(page: Page, options?: FreeformTutorialOptions): Promise<void>;

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
