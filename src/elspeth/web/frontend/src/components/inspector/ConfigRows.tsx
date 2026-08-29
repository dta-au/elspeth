// ============================================================================
// ConfigRows — the shared key/value renderer for node-inspector panels
// (extracted from GraphView.tsx so OptionRows.tsx can reuse it without
// creating an import cycle back into GraphView; elspeth-a6ea581e8a).
// ============================================================================

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function ConfigValue({ value }: { value: unknown }): JSX.Element {
  if (value === null) {
    return <span className="graph-config-empty-value">not set</span>;
  }
  if (Array.isArray(value)) {
    if (value.length === 0) {
      return <span className="graph-config-empty-value">empty list</span>;
    }
    return (
      <ul className="graph-config-list">
        {value.map((item, index) => (
          <li key={index}>
            <ConfigValue value={item} />
          </li>
        ))}
      </ul>
    );
  }
  if (isRecord(value)) {
    const entries = Object.entries(value);
    if (entries.length === 0) {
      return <span className="graph-config-empty-value">empty object</span>;
    }
    return (
      <dl className="graph-config-nested">
        {entries.map(([key, nestedValue]) => (
          <div key={key}>
            <dt>{key}</dt>
            <dd>
              <ConfigValue value={nestedValue} />
            </dd>
          </div>
        ))}
      </dl>
    );
  }
  if (typeof value === "boolean") {
    return <span>{value ? "true" : "false"}</span>;
  }
  return <span>{String(value)}</span>;
}

export function ConfigRows({
  values,
  emptyText,
}: {
  values: Record<string, unknown>;
  emptyText: string;
}): JSX.Element {
  const entries = Object.entries(values);
  if (entries.length === 0) {
    return <p className="graph-config-empty-value">{emptyText}</p>;
  }
  return (
    <dl className="graph-config-rows">
      {entries.map(([key, value]) => (
        <div key={key}>
          <dt>{key}</dt>
          <dd>
            <ConfigValue value={value} />
          </dd>
        </div>
      ))}
    </dl>
  );
}
