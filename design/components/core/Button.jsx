import React from "react";

/**
 * ELSPETH button. Composes the design-system `.btn` family.
 * Variants: primary (green CTA), danger (destructive), secondary (default
 * elevated surface), ghost (transparent). `compact` switches to the 36px
 * chrome-row size used in headers and dense toolbars.
 */
export function Button({
  variant = "secondary",
  compact = false,
  type = "button",
  disabled = false,
  iconLeft = null,
  iconRight = null,
  className = "",
  children,
  ...rest
}) {
  const base = compact ? "btn-compact" : "btn";
  const variantClass =
    variant === "primary"
      ? "btn-primary"
      : variant === "danger"
      ? "btn-danger"
      : variant === "ghost"
      ? "btn-ghost"
      : "";
  const cls = [base, variantClass, className].filter(Boolean).join(" ");
  return (
    <button type={type} className={cls} disabled={disabled} {...rest}>
      {iconLeft}
      {children}
      {iconRight}
    </button>
  );
}
