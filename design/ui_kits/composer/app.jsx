// app.jsx — orchestrates the ELSPETH Web Composer demo flow.
const { LoginScreen, ComposerHeader, ChatPanel, SideRail, CatalogDrawer, PipelineGraph, Icon } = window;

function YamlModal({ open, onClose, onCopy }) {
  if (!open) return null;
  const { Button } = window.ELSPETHDesignSystem_85edbb;
  return (
    <div style={{ position: "absolute", inset: 0, zIndex: 201, display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div onClick={onClose} style={{ position: "absolute", inset: 0, background: "rgba(0,0,0,0.45)" }} />
      <div style={{ position: "relative", width: "min(560px, calc(100% - 32px))", maxHeight: "80%", background: "var(--color-surface-paper)", border: "1px solid var(--color-border)", borderRadius: 8, boxShadow: "0 8px 32px rgba(0,0,0,0.25)", display: "flex", flexDirection: "column" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: 16, borderBottom: "1px solid var(--color-border)" }}>
          <div style={{ fontSize: 16, fontWeight: 600, color: "var(--color-text)" }}>Generated YAML</div>
          <button className="btn-compact" onClick={onClose} style={{ minWidth: 36 }}>×</button>
        </div>
        <pre style={{ flex: 1, overflow: "auto", margin: 0, padding: 16, fontFamily: "var(--font-mono)", fontSize: 12, lineHeight: 1.6, color: "var(--color-text-secondary)" }}>{window.ELSPETH_KIT.yaml}</pre>
        <div style={{ display: "flex", justifyContent: "flex-end", gap: 8, padding: 12, borderTop: "1px solid var(--color-border)" }}>
          <Button variant="secondary" onClick={onCopy}>Copy</Button>
          <Button variant="primary" onClick={onClose}>Done</Button>
        </div>
      </div>
    </div>
  );
}

function GraphModal({ open, onClose }) {
  if (!open) return null;
  return (
    <div style={{ position: "absolute", inset: 0, zIndex: 201, display: "flex", alignItems: "center", justifyContent: "center" }}>
      <div onClick={onClose} style={{ position: "absolute", inset: 0, background: "rgba(0,0,0,0.45)" }} />
      <div style={{ position: "relative", width: "min(840px, calc(100% - 32px))", background: "var(--color-surface)", border: "1px solid var(--color-border)", borderRadius: 8, boxShadow: "0 8px 32px rgba(0,0,0,0.25)" }}>
        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", padding: 16, borderBottom: "1px solid var(--color-border)" }}>
          <div style={{ fontSize: 16, fontWeight: 600, color: "var(--color-text)" }}>Execution graph</div>
          <button className="btn-compact" onClick={onClose} style={{ minWidth: 36 }}>×</button>
        </div>
        <div style={{ padding: 16 }}>
          <PipelineGraph nodes={window.ELSPETH_KIT.pipeline} variant="full" />
        </div>
      </div>
    </div>
  );
}

function Toast({ msg }) {
  if (!msg) return null;
  return (
    <div style={{ position: "absolute", bottom: 16, left: "50%", transform: "translateX(-50%)", zIndex: 301, padding: "8px 16px", borderRadius: 9999, background: "var(--color-surface-elevated)", border: "1px solid var(--color-border-strong)", color: "var(--color-text)", fontSize: 13, boxShadow: "0 2px 8px rgba(0,0,0,0.25)" }}>{msg}</div>
  );
}

function App() {
  const [screen, setScreen] = React.useState("login");
  const [theme, setTheme] = React.useState("dark");
  const [messages, setMessages] = React.useState([]);
  const [draft, setDraft] = React.useState("");
  const [composing, setComposing] = React.useState(false);
  const [pipelineCount, setPipelineCount] = React.useState(0);
  const [validated, setValidated] = React.useState(false);
  const [running, setRunning] = React.useState(false);
  const [runStatus, setRunStatus] = React.useState(null);
  const [catalogOpen, setCatalogOpen] = React.useState(false);
  const [yamlOpen, setYamlOpen] = React.useState(false);
  const [graphOpen, setGraphOpen] = React.useState(false);
  const [toast, setToast] = React.useState(null);

  React.useEffect(() => { document.documentElement.setAttribute("data-theme", theme); }, [theme]);

  function flashToast(m) { setToast(m); setTimeout(() => setToast(null), 1800); }
  const push = (msg) => setMessages((prev) => [...prev, msg]);

  function buildPipeline(userText) {
    push({ role: "user", text: userText });
    setComposing(true);
    setTimeout(() => {
      setComposing(false);
      push({
        role: "assistant",
        text: "I built a five-node pipeline: a CSV source for the submissions, an llm transform to classify each row, and a pure-config safety gate that routes high-risk rows (risk_score > 0.8) to a review queue and the rest to an approved sink.",
        tool: "set_pipeline",
        toolSummary: "Added source · transform · gate · 2 sinks. Every edge is explicitly named.",
        toolState: "committed",
      });
      setPipelineCount(5);
      setTimeout(() => {
        setComposing(true);
        setTimeout(() => {
          setComposing(false);
          push({ role: "system", text: "Preflight validation passed — graph, route targets, and edge/schema compatibility all check out." });
          setValidated(true);
        }, 900);
      }, 700);
    }, 1100);
  }

  function onSend(text) {
    setDraft("");
    if (pipelineCount === 0) { buildPipeline(text); return; }
    push({ role: "user", text });
    setComposing(true);
    setTimeout(() => {
      setComposing(false);
      push({ role: "assistant", text: "Done — that change is staged on the current pipeline version. Validate or run when you're ready.", tool: "update_node", toolSummary: "Patched the classify transform prompt.", toolState: "committed" });
    }, 1000);
  }

  function onPickTemplate(t) { buildPipeline("Build a " + t.title.toLowerCase() + " pipeline."); }

  function onRun() {
    setRunning(true);
    setRunStatus(null);
    setTimeout(() => {
      setRunning(false);
      setRunStatus("completed");
      push({ role: "system", text: "Run completed — 240 rows processed. 228 approved, 12 routed to review, 0 failed. Audit trail written to Landscape." });
    }, 1700);
  }

  function onTryPlugin(p) {
    setCatalogOpen(false);
    onSend("Add a " + p.name + " " + p.type + " to the pipeline.");
  }

  if (screen === "login") {
    return <div style={{ height: "100%" }}><LoginScreen onSignIn={() => setScreen("composer")} /><Toast msg={toast} /></div>;
  }

  return (
    <div style={{ display: "flex", flexDirection: "column", height: "100%", position: "relative" }}>
      <ComposerHeader sessionTitle="Tender evaluation" theme={theme} onToggleTheme={() => setTheme(theme === "dark" ? "light" : "dark")} onSignOut={() => { setScreen("login"); }} />
      <div style={{ flex: 1, minHeight: 0, display: "grid", gridTemplateColumns: "1fr 320px", position: "relative" }}>
        <div style={{ overflow: "hidden", borderRight: "1px solid var(--color-border)" }}>
          <ChatPanel
            messages={messages}
            composing={composing}
            empty={messages.length === 0}
            draft={draft}
            setDraft={setDraft}
            onSend={onSend}
            onPickTemplate={onPickTemplate}
          />
        </div>
        <SideRail
          pipelineCount={pipelineCount}
          validated={validated}
          running={running}
          runStatus={runStatus}
          onRun={onRun}
          onCopyYaml={() => { setYamlOpen(true); }}
          onSaveReview={() => flashToast("Share link copied — reviewers must sign in")}
          onOpenCatalog={() => setCatalogOpen(true)}
          onOpenGraph={() => setGraphOpen(true)}
        />
        <CatalogDrawer open={catalogOpen} onClose={() => setCatalogOpen(false)} onTry={onTryPlugin} />
      </div>
      <YamlModal open={yamlOpen} onClose={() => setYamlOpen(false)} onCopy={() => { setYamlOpen(false); flashToast("YAML copied to clipboard"); }} />
      <GraphModal open={graphOpen} onClose={() => setGraphOpen(false)} />
      <Toast msg={toast} />
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")).render(<App />);
