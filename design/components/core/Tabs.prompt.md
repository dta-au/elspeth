Underline tab strip — a muted row of tabs with a 2px accent underline on the active one. Used for catalog families and settings sections. Controlled.

```jsx
const [tab, setTab] = React.useState("sources");
<Tabs
  value={tab}
  onChange={setTab}
  tabs={[
    { id: "sources", label: "Sources", count: 6 },
    { id: "transforms", label: "Transforms", count: 14 },
    { id: "sinks", label: "Sinks", count: 7 },
  ]}
/>
```

Each tab is `{ id, label, count? }`; the optional count renders as a small pill that fills with accent when active.
