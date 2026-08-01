One row of the ELSPETH plugin catalog: name + component-type badge, a two-line description, and audit-characteristic chips (positive/attention/informational) that surface a plugin's assurance properties. Composes `TypeBadge`.

```jsx
<PluginCard
  name="Azure Blob"
  type="source"
  kind="azure_blob"
  description="Stream rows from an Azure Blob container. Parses CSV strictly; malformed rows are quarantined with an audit record."
  audit={[
    { label: "strict parsing", tone: "positive" },
    { label: "quarantine on error", tone: "informational" },
  ]}
  onTry={() => {}}
/>
```

Use inside the catalog drawer / `CatalogDrawer` list. `kind` overrides the badge label with the plugin id.
