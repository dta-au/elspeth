Branded button for ELSPETH — the `.btn` family with primary/secondary/danger/ghost variants and a compact chrome-row size. Use it for every clickable action; primary (green) is reserved for the main commit verb on a surface (Run, Sign in).

```jsx
<Button variant="primary" onClick={run}>Run</Button>
<Button variant="secondary">Copy YAML</Button>
<Button variant="danger">Delete</Button>
<Button variant="ghost" compact iconLeft={<i data-lucide="settings" />}>Settings</Button>
```

Variants: `primary` green CTA · `secondary` elevated default · `danger` red · `ghost` transparent. Props: `compact` (36px header size), `disabled`, `iconLeft`/`iconRight`, plus all native button attributes.
