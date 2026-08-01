import * as React from "react";

/**
 * Run-lifecycle status badge. ELSPETH distinguishes completed,
 * completed_with_failures (teal + ⚠), failed, empty (∅), cancelled, running,
 * and pending.
 *
 * @startingPoint section="Core" subtitle="Run lifecycle status pills" viewport="700x120"
 */
export interface StatusBadgeProps
  extends React.HTMLAttributes<HTMLSpanElement> {
  /** Run status. @default "pending" */
  status?:
    | "pending"
    | "running"
    | "completed"
    | "completed_with_failures"
    | "failed"
    | "empty"
    | "cancelled"
    | "cancelling";
  /** Override the label (defaults to the status string). */
  children?: React.ReactNode;
}

export function StatusBadge(props: StatusBadgeProps): React.ReactElement;
