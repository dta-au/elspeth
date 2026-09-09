import React from "react";

/**
 * Multiline text input. Composes `.textarea` (vertical resize). Optional
 * label + hint. Used for the chat composer input, prompt editing, and the
 * "I meant…" amend forms.
 */
export function Textarea({ label, hint, id, className = "", rows = 3, ...rest }) {
  const taId = id || (label ? `ta-${Math.random().toString(36).slice(2, 8)}` : undefined);
  const cls = ["textarea", className].filter(Boolean).join(" ");
  const control = <textarea id={taId} className={cls} rows={rows} {...rest} />;
  if (!label && !hint) return control;
  return (
    <div>
      {label ? (
        <label className="field-label" htmlFor={taId}>
          {label}
        </label>
      ) : null}
      {control}
      {hint ? <div className="field-hint">{hint}</div> : null}
    </div>
  );
}
