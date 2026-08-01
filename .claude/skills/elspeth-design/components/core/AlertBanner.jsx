import React from "react";

/**
 * Inline alert / status banner. Tone maps to ELSPETH semantic colours. Used for
 * backend-unavailable, service-unavailable, redirect toasts, etc. `action`
 * renders a right-aligned button (e.g. Retry, Configure API keys).
 */
export function AlertBanner({
  tone = "error",
  action = null,
  role,
  className = "",
  children,
  ...rest
}) {
  const toneClass =
    tone === "info"
      ? "alert-banner--info"
      : tone === "warning"
      ? "alert-banner--warning"
      : tone === "success"
      ? "alert-banner--success"
      : "";
  const cls = ["alert-banner", toneClass, className].filter(Boolean).join(" ");
  const resolvedRole = role ?? (tone === "error" ? "alert" : "status");
  return (
    <div className={cls} role={resolvedRole} {...rest}>
      <span>{children}</span>
      {action ? <span style={{ flexShrink: 0 }}>{action}</span> : null}
    </div>
  );
}
