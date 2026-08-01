import React from "react";

const GLYPH = {
  completed: null,
  completed_with_failures: "⚠",
  empty: "∅",
};

/**
 * Run-lifecycle status badge. Maps an ELSPETH terminal/lifecycle status to its
 * colour and (where used) functional glyph. completed_with_failures reuses the
 * teal "completed" colour and signals caveats with ⚠; empty uses a neutral
 * grey with ∅.
 */
export function StatusBadge({ status = "pending", className = "", children, ...rest }) {
  const colorKey =
    status === "completed_with_failures"
      ? "completed"
      : status === "cancelling"
      ? "cancelled"
      : status;
  const cls = ["status-badge", `status-badge-${colorKey}`, className]
    .filter(Boolean)
    .join(" ");
  const glyph = GLYPH[status];
  return (
    <span className={cls} {...rest}>
      {glyph ? <span aria-hidden="true">{glyph}</span> : null}
      {children ?? status}
    </span>
  );
}
