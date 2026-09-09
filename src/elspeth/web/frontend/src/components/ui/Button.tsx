import { forwardRef } from "react";
import type { ButtonHTMLAttributes, ReactNode } from "react";

/**
 * The canonical button primitive. Every `<button>` in the tree routes through
 * this component (migration census gate); use `variant="bare"` for controls
 * whose styling is entirely bespoke.
 *
 * ## `type` default — READ BEFORE MIGRATING A FORM BUTTON
 *
 * `<Button>` defaults to `type="button"`. A raw `<button>` inside a `<form>`
 * defaults to `type="submit"` — the OPPOSITE behaviour. Mechanically swapping
 * a form's submit button to `<Button>` therefore silently breaks submit-on-
 * click and submit-on-Enter. **Migrating a submit button REQUIRES an explicit
 * `type="submit"`.** The default is deliberate and stays: outside forms it
 * prevents accidental submits, which is the overwhelmingly common case.
 *
 * ## Variants
 *
 * - `"secondary"` (default) — base `.btn` (or `.btn-compact`) chrome, no
 *   modifier class.
 * - `"primary"` / `"danger"` / `"ghost"` — base chrome plus the matching
 *   `.btn-*` modifier.
 * - `"bare"` — NO base class and no modifier: only the caller's `className`
 *   is emitted (no `class` attribute at all when `className` is empty).
 *   `compact` is ignored under `"bare"` since it only selects between base
 *   classes. The `type="button"` default and the `iconLeft`/`children`/
 *   `iconRight` slots still apply. This is the route for genuinely bespoke
 *   controls (icon buttons, tabs, list-row triggers) so they can pass the
 *   census without visual churn.
 */
export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  /** Visual style; `"bare"` emits no base/modifier class. @default "secondary" */
  variant?: "primary" | "secondary" | "danger" | "ghost" | "bare";
  /** Use the 36px chrome-row size instead of the 44px default. Ignored when
   *  `variant="bare"`. @default false */
  compact?: boolean;
  iconLeft?: ReactNode;
  iconRight?: ReactNode;
}

export const Button = forwardRef<HTMLButtonElement, ButtonProps>(
  function Button(
    {
      variant = "secondary",
      compact = false,
      type = "button",
      iconLeft,
      iconRight,
      className = "",
      children,
      ...rest
    },
    ref,
  ) {
    const base = compact ? "btn-compact" : "btn";
    const variantClass =
      variant === "primary" ? "btn-primary"
      : variant === "danger" ? "btn-danger"
      : variant === "ghost" ? "btn-ghost"
      : "";
    const cls =
      variant === "bare"
        ? className
        : [base, variantClass, className].filter(Boolean).join(" ");
    return (
      <button ref={ref} type={type} className={cls || undefined} {...rest}>
        {iconLeft}
        {children}
        {iconRight}
      </button>
    );
  },
);

Button.displayName = "Button";
