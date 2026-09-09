// ComposerShell.jsx — header (navy) + chat panel (teal) + side rail (paper).
// Icon isolates lucide's DOM mutation from React. lucide replaces the
// <i data-lucide> with an <svg> behind React's back; if React ever reconciles
// that node it crashes (removeChild on a detached node). So we render a span
// React treats as EMPTY (no JSX children) and own its inner DOM via a ref —
// React never touches the lucide-mutated node, so name changes are safe.
function Icon({ name, size = 18, color }) {
  const ref = React.useRef(null);
  React.useEffect(() => {
    if (!ref.current) return;
    ref.current.innerHTML = "";
    const i = document.createElement("i");
    i.setAttribute("data-lucide", name);
    ref.current.appendChild(i);
    if (window.lucide) window.lucide.createIcons();
  }, [name, size, color]);
  return <span ref={ref} style={{ display: "inline-flex", width: size, height: size, color: color || "inherit" }} />;
}

/* ── Header ──────────────────────────────────────────────────────────────── */
function ComposerHeader({ sessionTitle, theme, onToggleTheme, onSignOut }) {
  const { WordMark } = window.ELSPETHDesignSystem_85edbb;
  return (
    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", height: 40, padding: "0 12px", borderBottom: "1px solid var(--color-border)", background: "var(--color-surface-nav)", flexShrink: 0 }}>
      <div style={{ display: "flex", alignItems: "center", gap: 12 }}>
        <WordMark size={13} />
        <div style={{ width: 1, height: 20, background: "var(--color-border)" }} />
        <button className="btn-compact" style={{ gap: 8 }}>
          {sessionTitle} <Icon name="chevron-down" size={14} />
        </button>
      </div>
      <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
        <button className="btn-compact" onClick={onToggleTheme} aria-label="Toggle theme" title="Toggle theme">
          <Icon name={theme === "dark" ? "sun" : "moon"} size={16} />
        </button>
        <button className="btn-compact" aria-label="Settings"><Icon name="settings" size={16} /></button>
        <button className="btn-compact" onClick={onSignOut} style={{ gap: 8 }}>
          <Icon name="user" size={14} /> Demo User
        </button>
      </div>
    </div>
  );
}

/* ── Composing indicator ─────────────────────────────────────────────────── */
function ComposingIndicator() {
  return (
    <div style={{ display: "flex", justifyContent: "flex-start", padding: "4px 16px" }}>
      <div style={{ padding: "10px 14px", borderRadius: "var(--radius-md)", background: "var(--color-bubble-assistant)", border: "1px solid var(--color-bubble-assistant-border)", display: "flex", gap: 8, alignItems: "center" }}>
        <span className="composing-dot" /><span className="composing-dot" /><span className="composing-dot" />
        <span style={{ fontSize: 12, color: "var(--color-text-muted)" }}>Working…</span>
      </div>
    </div>
  );
}

/* ── Tool-call card inside an assistant turn ─────────────────────────────── */
function ToolCallCard({ tool, summary, state }) {
  const accent = state === "committed" ? "var(--color-success)" : state === "rejected" ? "var(--color-error)" : "var(--color-warning)";
  return (
    <div style={{ marginTop: 8, padding: 8, border: "1px solid var(--color-border-strong)", borderLeft: `4px solid ${accent}`, borderRadius: "var(--radius-md)", background: "var(--color-surface-elevated)" }}>
      <div style={{ display: "flex", justifyContent: "space-between", gap: 8, alignItems: "center" }}>
        <span style={{ fontFamily: "var(--font-mono)", fontSize: 12, color: "var(--color-text)" }}>{tool}</span>
        <span style={{ fontSize: 10, textTransform: "uppercase", letterSpacing: ".06em", color: accent, fontWeight: 700 }}>{state}</span>
      </div>
      <div style={{ marginTop: 6, fontSize: 13, color: "var(--color-text-secondary)" }}>{summary}</div>
    </div>
  );
}

/* ── Empty-state template cards ──────────────────────────────────────────── */
function TemplateCards({ onPick }) {
  const items = [
    { icon: "scale", title: "Tender evaluation", sense: "CSV of submissions", decide: "LLM + safety gate", act: "Results, review queue" },
    { icon: "file-search", title: "Document QA", sense: "PDF / text blobs", decide: "Extraction, rubric checks", act: "Annotated outputs" },
    { icon: "shield-alert", title: "Content moderation", sense: "User submissions", decide: "Safety classifier", act: "Published, review, rejected" },
    { icon: "activity", title: "Threshold monitoring", sense: "Sensor feed", decide: "Threshold + anomaly", act: "Log, warning, alert" },
  ];
  return (
    <div style={{ padding: "24px 28px", maxWidth: 760, margin: "0 auto" }}>
      <div style={{ textAlign: "center", marginBottom: 22 }}>
        <h2 style={{ margin: "0 0 6px", fontSize: 22, fontWeight: 600, color: "var(--color-text)" }}>Build a pipeline</h2>
        <p style={{ margin: 0, fontSize: 15, color: "var(--color-text-muted)" }}>Describe what you want, or start from an example. Sense → Decide → Act.</p>
      </div>
      <div style={{ display: "grid", gridTemplateColumns: "repeat(2, 1fr)", gap: 12 }}>
        {items.map((t) => (
          <button key={t.title} onClick={() => onPick(t)} style={{ textAlign: "left", cursor: "pointer", display: "flex", flexDirection: "column", gap: 8, padding: 14, background: "var(--color-surface-elevated)", border: "1px solid var(--color-border)", borderRadius: "var(--radius-lg)", color: "var(--color-text)" }}>
            <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
              <Icon name={t.icon} size={18} color="var(--color-text-secondary)" />
              <span style={{ fontSize: 14, fontWeight: 600 }}>{t.title}</span>
            </div>
            <dl style={{ margin: 0, display: "grid", gap: 3, fontSize: 11, color: "var(--color-text-muted)" }}>
              <div><b style={{ color: "var(--color-badge-source)" }}>Sense</b> · {t.sense}</div>
              <div><b style={{ color: "var(--color-badge-transform)" }}>Decide</b> · {t.decide}</div>
              <div><b style={{ color: "var(--color-badge-sink)" }}>Act</b> · {t.act}</div>
            </dl>
          </button>
        ))}
      </div>
    </div>
  );
}

/* ── Chat panel ──────────────────────────────────────────────────────────── */
function ChatPanel({ messages, composing, empty, onSend, onPickTemplate, draft, setDraft }) {
  const { ChatBubble } = window.ELSPETHDesignSystem_85edbb;
  const endRef = React.useRef(null);
  React.useEffect(() => { if (endRef.current) endRef.current.scrollTop = endRef.current.scrollHeight; }, [messages.length, composing]);

  function submit(e) {
    e.preventDefault();
    if (!draft.trim()) return;
    onSend(draft.trim());
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflow: "hidden", background: "var(--color-surface)" }}>
      <div style={{ padding: "8px 16px", borderBottom: "1px solid var(--color-border)", display: "flex", alignItems: "center", gap: 12, flexShrink: 0 }}>
        <span style={{ fontSize: 13, fontWeight: 600, color: "var(--color-text)", flex: 1 }}>Tender evaluation</span>
        <button className="btn-compact"><Icon name="sparkles" size={14} /> Switch to guided</button>
      </div>

      <div ref={endRef} style={{ flex: 1, overflowY: "auto", padding: "16px 0" }}>
        {empty ? (
          <TemplateCards onPick={onPickTemplate} />
        ) : (
          <React.Fragment>
            {messages.map((m, i) => (
              <ChatBubble key={i} role={m.role}>
                {m.text}
                {m.tool ? <ToolCallCard tool={m.tool} summary={m.toolSummary} state={m.toolState || "committed"} /> : null}
              </ChatBubble>
            ))}
            {composing ? <ComposingIndicator /> : null}
          </React.Fragment>
        )}
      </div>

      <form onSubmit={submit} style={{ padding: "8px 16px", borderTop: "1px solid var(--color-border)", flexShrink: 0 }}>
        <div style={{ display: "flex", gap: 8, alignItems: "flex-end" }}>
          <button type="button" className="chat-input-icon-btn" aria-label="Attach" style={{ borderRadius: "var(--radius-md)" }}><Icon name="paperclip" size={18} /></button>
          <textarea
            className="textarea"
            data-chat-input
            rows={1}
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) submit(e); }}
            placeholder="Describe a change, or ask the composer to build a step…"
            style={{ flex: 1, minHeight: 44, resize: "none" }}
          />
          <button type="submit" className="chat-input-send-btn" disabled={!draft.trim()} aria-label="Send" style={{ display: "inline-flex", alignItems: "center", justifyContent: "center" }}>
            <Icon name="send" size={18} />
          </button>
        </div>
        <div style={{ fontSize: 11, color: "var(--color-text-muted)", textAlign: "right", padding: "2px 0 0" }}>Enter to send · Shift+Enter for newline</div>
      </form>
    </div>
  );
}

/* ── Side rail (inspection / paper) ──────────────────────────────────────── */
function SideRail({ pipelineCount, validated, runStatus, running, onRun, onCopyYaml, onSaveReview, onOpenCatalog, onOpenGraph }) {
  const { StatusBadge, Button } = window.ELSPETHDesignSystem_85edbb;
  const nodes = window.ELSPETH_KIT.pipeline;
  const checks = [
    { label: "Graph structure", ok: pipelineCount >= 5 },
    { label: "Route targets", ok: pipelineCount >= 5 },
    { label: "Edge / schema compatibility", ok: validated },
    { label: "Secret references resolved", ok: validated },
  ];
  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", overflowY: "auto", background: "var(--color-surface-inspector)" }}>
      <div style={{ padding: "12px 12px 0" }}>
        <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: ".08em", color: "var(--color-text-muted)", marginBottom: 8 }}>Pipeline graph</div>
        <div onClick={pipelineCount ? onOpenGraph : undefined} style={{ cursor: pipelineCount ? "pointer" : "default" }}>
          <PipelineGraph nodes={nodes} count={pipelineCount} variant="mini" />
        </div>
      </div>

      {/* Validation banner */}
      <div style={{ padding: "10px 12px 0" }}>
        {validated ? (
          <div className="validation-banner validation-banner-pass">✓ Validation passed — 0 errors</div>
        ) : pipelineCount >= 5 ? (
          <div className="validation-banner" style={{ background: "var(--color-warning-bg)", border: "1px solid var(--color-warning-border)", color: "var(--color-warning)" }}>Ready to validate — run preflight</div>
        ) : (
          <div className="validation-banner" style={{ background: "var(--color-surface)", border: "1px solid var(--color-border)", color: "var(--color-text-muted)" }}>Build a pipeline to validate</div>
        )}
      </div>

      {/* Audit readiness */}
      <div style={{ padding: "14px 12px 0" }}>
        <div style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", letterSpacing: ".08em", color: "var(--color-text-muted)", marginBottom: 8 }}>Audit readiness</div>
        <div style={{ display: "grid", gap: 6 }}>
          {checks.map((c) => (
            <div key={c.label} style={{ display: "flex", alignItems: "center", gap: 8, fontSize: 12, color: "var(--color-text-secondary)" }}>
              <Icon name={c.ok ? "check-circle-2" : "circle-dashed"} size={15} color={c.ok ? "var(--color-success)" : "var(--color-text-muted)"} />
              {c.label}
            </div>
          ))}
        </div>
      </div>

      {/* Catalog button */}
      <div style={{ padding: "14px 12px 0" }}>
        <button onClick={onOpenCatalog} style={{ width: "100%", minHeight: 44, padding: 8, display: "grid", gridTemplateColumns: "1fr auto", alignItems: "center", gap: 8, border: "1px solid var(--color-border)", borderRadius: "var(--radius-sm)", background: "var(--color-surface)", color: "var(--color-text)", cursor: "pointer", textAlign: "left" }}>
          <span style={{ fontSize: 13, fontWeight: 650 }}>Plugin catalog</span>
          <span style={{ fontSize: 10, fontWeight: 700, textTransform: "uppercase", color: "var(--color-text-muted)", border: "1px solid var(--color-border)", borderRadius: 4, padding: "2px 6px" }}>⌘⇧P</span>
        </button>
      </div>

      {/* Run result */}
      {runStatus ? (
        <div style={{ margin: "14px 12px 0", padding: 10, border: "1px solid var(--color-border)", borderRadius: "var(--radius-md)", background: "var(--color-surface)" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 6 }}>
            <span style={{ fontSize: 11, fontWeight: 700, color: "var(--color-text-secondary)", textTransform: "uppercase", letterSpacing: ".06em" }}>Last run</span>
            <StatusBadge status={runStatus} />
          </div>
          <div style={{ fontSize: 12, color: "var(--color-text-muted)", fontFamily: "var(--font-mono)" }}>
            240 rows · 228 approved · 12 → review · 0 failed
          </div>
        </div>
      ) : null}

      {/* Completion bar */}
      <div style={{ marginTop: "auto", display: "flex", flexDirection: "column", gap: 8, padding: 12 }}>
        <Button variant="secondary" onClick={onSaveReview} style={{ width: "100%" }}>Save for review</Button>
        <Button variant="primary" disabled={!validated || running} onClick={onRun} style={{ width: "100%" }}>
          {running ? "Running…" : "Run"}
        </Button>
        <Button variant="secondary" onClick={onCopyYaml} style={{ width: "100%" }}>Copy YAML</Button>
      </div>
    </div>
  );
}

Object.assign(window, { ComposerHeader, ChatPanel, SideRail, Icon });
