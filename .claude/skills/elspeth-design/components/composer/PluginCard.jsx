import React from "react";
import { TypeBadge } from "../core/TypeBadge.jsx";

/**
 * Plugin catalog card — one entry in the ELSPETH plugin catalog drawer. Shows
 * the plugin name, its component type, a clamped description, and a strip of
 * audit-characteristic chips (positive / attention / informational).
 */
export function PluginCard({
  name,
  type = "source",
  kind,
  description,
  audit = [],
  onTry,
  ...rest
}) {
  return (
    <div
      style={{
        padding: "var(--space-md)",
        borderBottom: "1px solid var(--color-border)",
        background: "var(--color-surface)",
      }}
      {...rest}
    >
      <div style={{ display: "flex", alignItems: "baseline", justifyContent: "space-between", gap: "var(--space-sm)" }}>
        <span style={{ fontWeight: 700, fontSize: "var(--font-size-sm)", color: "var(--color-text)" }}>{name}</span>
        <TypeBadge type={type}>{kind ?? type}</TypeBadge>
      </div>
      {description ? (
        <div
          style={{
            marginTop: 2,
            fontSize: "var(--font-size-xs)",
            color: "var(--color-text-muted)",
            lineHeight: 1.35,
          }}
        >
          {description}
        </div>
      ) : null}
      {audit.length ? (
        <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginTop: "var(--space-xs)" }}>
          {audit.map((a, i) => (
            <AuditChip key={i} tone={a.tone}>{a.label}</AuditChip>
          ))}
        </div>
      ) : null}
      {onTry ? (
        <div style={{ display: "flex", gap: "var(--space-xs)", marginTop: "var(--space-sm)" }}>
          <button className="btn-compact" style={{ minHeight: 30, fontSize: "var(--font-size-xs)" }} onClick={onTry}>
            Try in composer
          </button>
        </div>
      ) : null}
    </div>
  );
}

function AuditChip({ tone = "informational", children }) {
  const toneStyle = {
    positive: { color: "var(--color-success)", borderColor: "var(--color-success-border)", background: "var(--color-success-bg)" },
    attention: { color: "var(--color-warning)", borderColor: "var(--color-warning-border)", background: "var(--color-warning-bg)" },
    informational: { color: "var(--color-info)", borderColor: "var(--color-info-border)", background: "var(--color-info-bg)" },
  }[tone] || {};
  return (
    <span
      style={{
        display: "inline-flex",
        alignItems: "center",
        minHeight: 22,
        padding: "2px 6px",
        border: "1px solid var(--color-border)",
        borderRadius: "var(--radius-sm)",
        fontSize: "var(--font-size-3xs)",
        fontWeight: 650,
        lineHeight: 1.2,
        ...toneStyle,
      }}
    >
      {children}
    </span>
  );
}
