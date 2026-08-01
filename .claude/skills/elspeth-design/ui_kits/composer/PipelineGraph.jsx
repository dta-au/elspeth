// PipelineGraph.jsx — the Sense→Decide→Act node flow, on a faint dot-grid canvas.
const pgBadgeClass = {
  source: "type-badge-source",
  transform: "type-badge-transform",
  gate: "type-badge-gate",
  sink: "type-badge-sink",
  aggregation: "type-badge-aggregation",
  coalesce: "type-badge-coalesce",
};
const pgNodeColor = {
  source: "var(--color-badge-source)",
  transform: "var(--color-badge-transform)",
  gate: "var(--color-badge-gate)",
  sink: "var(--color-badge-sink)",
};

function PipelineNode({ node, variant }) {
  const compact = variant === "mini";
  return (
    <div
      style={{
        flex: "0 0 auto",
        minWidth: compact ? 78 : 150,
        padding: compact ? "6px 8px" : "10px 12px",
        background: "var(--color-surface)",
        border: "1px solid var(--color-border-strong)",
        borderLeft: `3px solid ${pgNodeColor[node.type] || "var(--color-border-strong)"}`,
        borderRadius: "var(--radius-md)",
      }}
    >
      {compact ? (
        <span className={"type-badge " + pgBadgeClass[node.type]} style={{ fontSize: 9, padding: "1px 5px" }}>
          {node.label}
        </span>
      ) : (
        <React.Fragment>
          <span className={"type-badge " + pgBadgeClass[node.type]}>{node.label}</span>
          <div style={{ marginTop: 7, fontSize: 13, fontWeight: 600, color: "var(--color-text)" }}>{node.title}</div>
          <div style={{ marginTop: 2, fontSize: 11, color: "var(--color-text-muted)", fontFamily: "var(--font-mono)" }}>{node.sub}</div>
        </React.Fragment>
      )}
    </div>
  );
}

function PipelineGraph({ nodes, count = 99, variant = "full" }) {
  const compact = variant === "mini";
  const shown = nodes.slice(0, count);
  const gridBg = {
    backgroundImage: "radial-gradient(var(--color-canvas-grid) 1px, transparent 1px)",
    backgroundSize: compact ? "12px 12px" : "18px 18px",
  };
  if (shown.length === 0) {
    return (
      <div style={{ ...gridBg, display: "flex", alignItems: "center", justifyContent: "center", height: compact ? 96 : 160, color: "var(--color-text-muted)", fontSize: 13, borderRadius: "var(--radius-md)" }}>
        No pipeline yet
      </div>
    );
  }
  // Split last two sinks onto a branch for the full view to show the gate fan-out.
  return (
    <div style={{ ...gridBg, padding: compact ? 8 : 16, borderRadius: "var(--radius-md)", overflowX: "auto" }}>
      <div style={{ display: "flex", alignItems: "center", gap: compact ? 6 : 12 }}>
        {shown.map((n, i) => (
          <React.Fragment key={n.id}>
            {i > 0 ? (
              <span style={{ flex: "0 0 auto", color: "var(--color-border-strong)", fontSize: compact ? 12 : 18 }}>→</span>
            ) : null}
            <PipelineNode node={n} variant={variant} />
          </React.Fragment>
        ))}
      </div>
    </div>
  );
}

Object.assign(window, { PipelineGraph });
