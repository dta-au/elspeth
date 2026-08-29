Surface container with the ELSPETH 1px teal border + 8px radius. Elevation comes from border + surface, not big shadows. Use `paper` for right-rail / modal panels (warm-neutral inspection family).

```jsx
<Card>
  <CardHeader title="Audit readiness" eyebrow="Side rail" actions={<Button compact>Refresh</Button>} />
  <p>…</p>
</Card>

<Card paper>Inspection panel content</Card>
```

Props: `paper` (warm-neutral surface), `pad` (set false for full-bleed). `CardHeader` gives a title + uppercase eyebrow + right-aligned actions slot.
