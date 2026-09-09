import React from "react";

/**
 * Surface card. `paper` switches to the warm-neutral inspection family used by
 * right-rail and modal panels. `pad` toggles default padding off for media.
 */
export function Card({ paper = false, pad = true, className = "", style, children, ...rest }) {
  const cls = ["card", paper ? "card-paper" : "", className].filter(Boolean).join(" ");
  return (
    <div className={cls} style={{ ...(pad ? null : { padding: 0 }), ...style }} {...rest}>
      {children}
    </div>
  );
}

/** Optional header row for a Card: title + right-aligned actions slot. */
export function CardHeader({ title, actions = null, eyebrow = null }) {
  return (
    <div
      style={{
        display: "flex",
        alignItems: "flex-start",
        justifyContent: "space-between",
        gap: "var(--space-sm)",
        marginBottom: "var(--space-md)",
      }}
    >
      <div style={{ minWidth: 0 }}>
        {eyebrow ? (
          <div
            style={{
              fontSize: "var(--font-size-3xs)",
              fontWeight: 700,
              textTransform: "uppercase",
              letterSpacing: "0.08em",
              color: "var(--color-text-muted)",
              marginBottom: 2,
            }}
          >
            {eyebrow}
          </div>
        ) : null}
        <div style={{ fontSize: "var(--font-size-base)", fontWeight: 700, color: "var(--color-text)" }}>
          {title}
        </div>
      </div>
      {actions}
    </div>
  );
}
