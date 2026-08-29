import React from "react";

const TYPES = ["source", "transform", "gate", "sink", "aggregation", "coalesce"];

/**
 * Component-type badge — the fixed colour vocabulary for the six ELSPETH
 * pipeline primitives. Mono typeface, uppercase, used in catalog cards,
 * graph nodes, and validation messages.
 */
export function TypeBadge({ type = "source", className = "", children, ...rest }) {
  const t = TYPES.includes(type) ? type : "source";
  const cls = ["type-badge", `type-badge-${t}`, className].filter(Boolean).join(" ");
  return (
    <span className={cls} {...rest}>
      {children ?? t}
    </span>
  );
}
