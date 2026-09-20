import type { Page } from "@playwright/test";

export const ACKNOWLEDGEMENT_PRIMARY_ACTION_NAMES: RegExp[];
export const GUIDED_STAGE_PRIMARY_ACTION_NAMES: string[];
export const STAGED_GUIDED_PHASES: string[];

export function isComposeRequest(url: string, method: string): boolean;
export function isRunRequest(url: string, method: string): boolean;
export function resolveVisibleReviews(page: Page): Promise<number>;

export interface StagedGuidedTutorialOptions {
  timeoutMs?: number;
  decisionScreenshotPath?: string | null;
}

export function driveStagedGuidedTutorial(
  page: Page,
  options?: StagedGuidedTutorialOptions,
): Promise<void>;
