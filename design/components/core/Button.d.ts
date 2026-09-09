import * as React from "react";

/**
 * ELSPETH button — composes the `.btn` family. Primary is the green CTA used
 * for Run/Execute; danger for destructive actions; ghost for quiet toolbar
 * actions. Use `compact` inside the 40px header and dense toolbars.
 *
 * @startingPoint section="Core" subtitle="Primary, secondary, danger, ghost — 44px / 36px" viewport="700x150"
 */
export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement> {
  /** Visual style. @default "secondary" */
  variant?: "primary" | "secondary" | "danger" | "ghost";
  /** Use the 36px chrome-row size instead of the 44px default. @default false */
  compact?: boolean;
  /** Icon node rendered before the label. */
  iconLeft?: React.ReactNode;
  /** Icon node rendered after the label. */
  iconRight?: React.ReactNode;
}

export function Button(props: ButtonProps): React.ReactElement;
