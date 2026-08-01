Status pill for an ELSPETH run's lifecycle. Colour + optional glyph encode the outcome; use it in run history, the side rail, and inline run results.

```jsx
<StatusBadge status="running" />
<StatusBadge status="completed_with_failures" />
<StatusBadge status="empty" />
```

Statuses: `pending` `running` `completed` `completed_with_failures` (teal + ⚠) `failed` `empty` (grey + ∅) `cancelled`. Keep the snake_case status names — they mirror the engine's terminal model.
