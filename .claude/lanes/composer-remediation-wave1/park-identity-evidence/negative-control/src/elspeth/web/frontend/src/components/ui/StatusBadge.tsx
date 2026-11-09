import type { HTMLAttributes, ReactNode } from "react";

const GLYPH: Partial<Record<string, string>> = {
  completed_with_failures: "⚠",
  empty: "∅",
};

export interface StatusBadgeProps extends HTMLAttributes<HTMLSpanElement> {
  status?:
    | "pending" | "running" | "completed" | "completed_with_failures"
    | "failed" | "empty" | "cancelled" | "cancelling";
  children?: ReactNode;
}

export function StatusBadge({ status = "pending", className = "", children, ...rest }: StatusBadgeProps) {
  // completed_with_failures is NOT aliased to completed (elspeth-cd885f4c4d):
  // the progress bar, the app toast and the Run-tab dot all render it in the
  // warning family, and the badge was the sole surface calling a partial
  // failure an unqualified success. It keeps its own class (shared.css) in
  // the same warning family, plus the ⚠ glyph.
  const colorKey = status === "cancelling" ? "cancelled" : status;
  const cls = ["status-badge", `status-badge-${colorKey}`, className].filter(Boolean).join(" ");
  const glyph = GLYPH[status];
  return (
    <span className={cls} {...rest}>
      {glyph ? <span aria-hidden="true">{glyph}</span> : null}
      {children ?? status}
    </span>
  );
}
