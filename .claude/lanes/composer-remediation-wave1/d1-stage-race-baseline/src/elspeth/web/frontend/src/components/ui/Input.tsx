import { forwardRef, useId } from "react";
import type { InputHTMLAttributes, ReactNode } from "react";

export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  /** Field label rendered above the control. */
  label?: ReactNode;
  /** Helper text rendered below the control. */
  hint?: ReactNode;
  /** Render the value in JetBrains Mono. @default false */
  mono?: boolean;
  /**
   * Suppress the `.input` text-field chrome and emit `className` verbatim
   * (`mono` is ignored). For text-like fields with a complete bespoke recipe.
   * @default false
   */
  bare?: boolean;
}

/** Input types that are not text fields and must not get `.input` chrome. */
const NON_TEXT_INPUT_TYPES = new Set([
  "checkbox",
  "radio",
  "range",
  "file",
  "color",
]);

export const Input = forwardRef<HTMLInputElement, InputProps>(function Input(
  { label, hint, mono = false, bare = false, id, className = "", ...rest },
  ref,
) {
  const generatedId = useId();
  const inputId = id ?? generatedId;
  // A hint that is not referenced is never announced: a screen-reader user
  // gets the label and nothing else. Merged with the caller's own
  // aria-describedby, which comes first so its reading order is unchanged.
  const hintId = `${inputId}-hint`;
  const describedBy = hint
    ? [rest["aria-describedby"], hintId].filter(Boolean).join(" ")
    : rest["aria-describedby"];
  const nonText =
    rest.type !== undefined && NON_TEXT_INPUT_TYPES.has(rest.type);
  const cls = [
    bare || nonText ? "" : "input",
    !bare && mono ? "input-mono" : "",
    className,
  ]
    .filter(Boolean)
    .join(" ");
  const control = (
    <input ref={ref} id={inputId} className={cls || undefined} {...rest} aria-describedby={describedBy} />
  );
  if (!label && !hint) return control;
  return (
    <div>
      {label ? (
        <label className="field-label" htmlFor={inputId}>
          {label}
        </label>
      ) : null}
      {control}
      {hint ? <div id={hintId} className="field-hint">{hint}</div> : null}
    </div>
  );
});

Input.displayName = "Input";
