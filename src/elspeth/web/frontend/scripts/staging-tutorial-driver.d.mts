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
