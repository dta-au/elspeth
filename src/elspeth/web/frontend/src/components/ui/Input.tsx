import { forwardRef, useId } from "react";
import type { InputHTMLAttributes, ReactNode } from "react";

/**
 * The canonical `<input>` primitive. Text-shaped types render on the ELSPETH
 * elevated surface with a strong border and visible focus ring. Optional
 * label + hint. `mono` switches to JetBrains Mono for forensic-register
 * values (secret names, paths, hashes). Pair with `label`/`hint` or use the
 * bare control inside your own field layout.
 *
 * ## Non-text types — automatic, driven by `type`
 *
 * `type="checkbox" | "radio" | "range" | "file" | "color"` are not text
 * fields, so for exactly those five types the `.input` text-field base class
 * is OMITTED automatically — the control carries only the caller's
 * `className` (plus `input-mono` if explicitly set). There is no `bare`
 * prop: the `type` prop is the single source of truth, so migrating a raw
 * `<input type="checkbox">` to `<Input type="checkbox">` is mechanical and
 * visually inert. All other types (including the default, `type` omitted)
 * receive `.input`.
 *
 * The label is associated with the control via the passed `id`; when no `id`
 * is supplied one is generated with React `useId()` so the label/control pair
 * is always programmatically linked. This holds for non-text types too.
 */
export interface InputProps extends InputHTMLAttributes<HTMLInputElement> {
  /** Field label rendered above the control. */
  label?: ReactNode;
  /** Helper text rendered below the control. */
  hint?: ReactNode;
  /** Render the value in JetBrains Mono. @default false */
  mono?: boolean;
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
  { label, hint, mono = false, id, className = "", ...rest },
  ref,
) {
  const generatedId = useId();
  const inputId = id ?? generatedId;
  const nonText =
    rest.type !== undefined && NON_TEXT_INPUT_TYPES.has(rest.type);
  const cls = [nonText ? "" : "input", mono ? "input-mono" : "", className]
    .filter(Boolean)
    .join(" ");
  const control = (
    <input ref={ref} id={inputId} className={cls || undefined} {...rest} />
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
      {hint ? <div className="field-hint">{hint}</div> : null}
    </div>
  );
});

Input.displayName = "Input";
