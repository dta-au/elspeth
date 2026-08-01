import type { HTMLAttributes, ReactNode } from "react";

export interface TypeBadgeProps extends HTMLAttributes<HTMLSpanElement> {
  /** Which pipeline primitive. @default "source" */
  type?:
    | "source"
    | "transform"
    | "gate"
    | "sink"
    | "aggregation"
    | "coalesce"
    | "row_union"
    | "queue";
  /** Override the label (defaults to the type name). */
  children?: ReactNode;
}

export function TypeBadge({ type = "source", className = "", children, ...rest }: TypeBadgeProps) {
  const cls = ["type-badge", `type-badge-${type}`, className].filter(Boolean).join(" ");
  const defaultLabel = type === "row_union" ? "row union" : type;
  return (
    <span className={cls} {...rest}>
      {children ?? defaultLabel}
    </span>
  );
}
