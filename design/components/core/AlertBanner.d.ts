import * as React from "react";

/**
 * Inline alert / status banner in ELSPETH semantic tones. Error banners get
 * role="alert" (assertive); softer tones get role="status" (polite) by
 * default. Copy should be specific and operator-actionable.
 *
 * @startingPoint section="Core" subtitle="Semantic alert / status banners" viewport="700x150"
 */
export interface AlertBannerProps
  extends Omit<React.HTMLAttributes<HTMLDivElement>, "role"> {
  /** Semantic tone. @default "error" */
  tone?: "error" | "warning" | "info" | "success";
  /** Right-aligned action (e.g. a Retry button). */
  action?: React.ReactNode;
  /** Override the inferred ARIA role. */
  role?: string;
}

export function AlertBanner(props: AlertBannerProps): React.ReactElement;
