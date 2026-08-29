// CatalogDrawer.jsx — the plugin catalog drawer that slides in from the right.
function CatalogDrawer({ open, onClose, onTry }) {
  const { PluginCard, Tabs, Input } = window.ELSPETHDesignSystem_85edbb;
  const cat = window.ELSPETH_KIT.catalog;
  const [tab, setTab] = React.useState("sources");
  const [q, setQ] = React.useState("");
  if (!open) return null;

  const list = (cat[tab] || []).filter(
    (p) => p.name.toLowerCase().includes(q.toLowerCase()) || (p.kind || "").includes(q.toLowerCase())
  );

  return (
    <React.Fragment>
      <div onClick={onClose} style={{ position: "absolute", inset: 0, background: "rgba(0,0,0,0.3)", zIndex: 40 }} />
      <div
        style={{
          position: "absolute", top: 0, right: 0, bottom: 0, width: "min(440px, calc(100% - 24px))", zIndex: 41,
          background: "var(--color-surface)", borderLeft: "1px solid var(--color-border)",
          display: "flex", flexDirection: "column",
        }}
      >
        <div style={{ display: "flex", alignItems: "flex-start", justifyContent: "space-between", gap: 8, padding: 16, borderBottom: "1px solid var(--color-border)" }}>
          <div>
            <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: ".08em", color: "var(--color-text-muted)" }}>Plugin catalog</div>
            <div style={{ fontSize: 16, fontWeight: 700, color: "var(--color-text)" }}>Sources, transforms &amp; sinks</div>
            <div style={{ fontSize: 12, color: "var(--color-text-muted)", marginTop: 2 }}>Discovered via pluggy. Each shows its audit characteristics.</div>
          </div>
          <button className="btn-compact" onClick={onClose} aria-label="Close catalog" style={{ minWidth: 36 }}>×</button>
        </div>

        <div style={{ padding: "8px 16px", borderBottom: "1px solid var(--color-border)" }}>
          <Input mono value={q} onChange={(e) => setQ(e.target.value)} placeholder="Search plugins…" />
        </div>

        <Tabs
          value={tab}
          onChange={setTab}
          tabs={[
            { id: "sources", label: "Sources", count: cat.sources.length },
            { id: "transforms", label: "Transforms", count: cat.transforms.length },
            { id: "sinks", label: "Sinks", count: cat.sinks.length },
          ]}
        />

        <div style={{ flex: 1, overflowY: "auto" }}>
          {list.length === 0 ? (
            <div style={{ padding: 16, fontSize: 12, color: "var(--color-text-muted)" }}>No plugins match “{q}”.</div>
          ) : (
            list.map((p) => (
              <PluginCard key={p.kind + p.name} name={p.name} type={p.type} kind={p.kind} description={p.description} audit={p.audit} onTry={() => onTry(p)} />
            ))
          )}
        </div>
      </div>
    </React.Fragment>
  );
}

Object.assign(window, { CatalogDrawer });
