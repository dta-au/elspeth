Full-width inline banner for outages and notices, in ELSPETH semantic tones. Error → assertive `role="alert"`; info/warning/success → polite `role="status"`. Keep copy specific ("Backend unavailable — Cannot connect to the ELSPETH server"), never vague.

```jsx
<AlertBanner tone="error" action={<Button compact onClick={retry}>Retry</Button>}>
  <strong>Backend unavailable</strong> — Cannot connect to the ELSPETH server.
</AlertBanner>
<AlertBanner tone="info">Service unavailable: the composer cannot reach a usable LLM right now.</AlertBanner>
```

Tones: `error` `warning` `info` `success`. `action` slot is right-aligned.
