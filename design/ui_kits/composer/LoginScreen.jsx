// LoginScreen.jsx — centred local-auth card, "Sign in to ELSPETH".
function LoginScreen({ onSignIn }) {
  const { WordMark, Input, Button } = window.ELSPETHDesignSystem_85edbb;
  const [u, setU] = React.useState("demo");
  const [p, setP] = React.useState("demo12345");
  const [busy, setBusy] = React.useState(false);

  function submit(e) {
    e.preventDefault();
    setBusy(true);
    setTimeout(() => { setBusy(false); onSignIn(); }, 480);
  }

  return (
    <div style={{ display: "flex", alignItems: "center", justifyContent: "center", height: "100%", background: "var(--color-bg)" }}>
      <div style={{ width: 360, padding: 32, background: "var(--color-surface)", borderRadius: 8, border: "1px solid var(--color-border)", boxShadow: "0 2px 8px rgba(10,40,50,0.4)" }}>
        <div style={{ textAlign: "center", marginBottom: 8 }}>
          <WordMark size={20} />
        </div>
        <h1 style={{ fontSize: 18, fontWeight: 600, margin: "0 0 24px", textAlign: "center", color: "var(--color-text)" }}>
          Sign in to ELSPETH
        </h1>
        <form onSubmit={submit}>
          <div style={{ marginBottom: 16 }}>
            <Input label="Username" value={u} onChange={(e) => setU(e.target.value)} autoComplete="username" />
          </div>
          <div style={{ marginBottom: 24 }}>
            <Input label="Password" type="password" value={p} onChange={(e) => setP(e.target.value)} autoComplete="current-password" />
          </div>
          <Button type="submit" variant="primary" disabled={busy} style={{ width: "100%" }}>
            {busy ? "Signing in…" : "Sign in"}
          </Button>
        </form>
        <p style={{ marginTop: 16, fontSize: 11, color: "var(--color-text-muted)", textAlign: "center" }}>
          Local development credentials. Do not reuse outside local dev.
        </p>
      </div>
    </div>
  );
}

Object.assign(window, { LoginScreen });
