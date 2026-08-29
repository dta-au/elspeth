Text input on the ELSPETH elevated surface — strong 1px border, 6px radius, white focus ring. Optional `label` and `hint` wrap it in a field; `mono` is for secret names, paths, and hashes.

```jsx
<Input label="Username" autoComplete="username" />
<Input label="Secret name" mono placeholder="AZURE_OPENAI_API_KEY" hint="Referenced as $secret{name}" />
```

Accepts all native input attributes. Omit `label`/`hint` to get the bare control for custom field layouts.
