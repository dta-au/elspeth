import React from "react";

/**
 * Text input. Composes `.input`. `mono` switches to JetBrains Mono for secret
 * names, paths, and other forensic-register values. Pair with `label`/`hint`
 * or use the bare control inside your own field layout.
 */
export function Input({ label, hint, mono = false, id, className = "", ...rest }) {
  const inputId = id || (label ? `inp-${Math.random().toString(36).slice(2, 8)}` : undefined);
  const cls = ["input", mono ? "input-mono" : "", className].filter(Boolean).join(" ");
  const control = <input id={inputId} className={cls} {...rest} />;
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
}
