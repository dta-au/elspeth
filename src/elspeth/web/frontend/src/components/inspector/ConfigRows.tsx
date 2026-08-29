// ============================================================================
// ConfigRows — the shared key/value renderer for node-inspector panels
// (extracted from GraphView.tsx so OptionRows.tsx can reuse it without
// creating an import cycle back into GraphView; elspeth-a6ea581e8a).
// ============================================================================

// `titleCaseLabel` only (not OptionRows' `optionLabel`, which also checks a
// curated OPTION_LABELS map): OptionRows.tsx imports ConfigRows/ConfigValue
// from this module, so importing OptionRows back here would be a cycle.
// `titleCaseLabel` has no such dependency (leaf module).
import { titleCaseLabel } from "@/components/catalog/pluginDisplayName";

// Option keys whose value is a STRUCTURE ELSPETH defines (a schema
// descriptor), never reader-authored data. Grepped across every plugin's
// option model (`grep -rn "schema:" src/elspeth/plugins`): every source,
// sink, and transform that takes a schema config exposes it as the option
// key `schema`, always shaped `{mode, guaranteed_fields, ...}` — genuinely
// ELSPETH-owned sub-keys. `output_schema`/`input_schema` are not (yet) a
// literal composer option key on any shipped plugin — they exist today only
// as runtime-derived attributes (`self.input_schema = ...`,
// `plugins/infrastructure/base.py`) built FROM `schema` — but they are the
// same SchemaConfig-shaped family, named in this ticket's own suggested
// allowlist, and already exercised by GraphView.test.tsx's existing
// structured-output fixture; kept here defensively for that shape.
//
// This is a FAIL-CLOSED allowlist, not a denylist (elspeth-b9ebdf9011 round
// 3): nested-key humanising is OFF by default (`humaniseKeys` below
// defaults to `false`) and turns on only for a value nested directly under
// one of these container keys. A user-KEYED option — `field_mapping`
// (`dict[reader's column name, target name]`, every tabular source/sink),
// `lookups` (Dataverse's `dict[pipeline field, lookup binding]`),
// `headers_custom_mapping` (`dict[str, str]` display overrides) — must
// default to verbatim: its keys ARE the reader's own data, not ours to
// relabel. Do not add a key here without confirming (via grep, not
// inference) that ELSPETH — not the reader — names its sub-keys.
export const STRUCTURAL_OPTION_CONTAINER_KEYS: ReadonlySet<string> = new Set([
  "schema",
  "output_schema",
  "input_schema",
]);

export function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

export function ConfigValue({
  value,
  humaniseKeys = false,
}: {
  value: unknown;
  /** Humanise this value's own object keys (and, recursively, every nested
   *  object's keys beneath it) via `titleCaseLabel`. Defaults to `false` —
   *  fail closed: a value reached with no explicit opt-in renders every key
   *  verbatim, so a newly added user-keyed option is safe by default. Set
   *  only when the caller has confirmed (via STRUCTURAL_OPTION_CONTAINER_KEYS
   *  or an equivalent check) that this subtree's keys are ELSPETH's own
   *  schema vocabulary, not reader data. */
  humaniseKeys?: boolean;
}): JSX.Element {
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
            <ConfigValue value={item} humaniseKeys={humaniseKeys} />
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
            {/* Structural keys inside a nested value (e.g. a source's
                `schema.guaranteed_fields`) are the shape ELSPETH owns, not
                user data — humanise them the same way OptionRows humanises
                top-level option keys (copy register: no snake_case in
                visible text), but ONLY when the caller has opted in via
                `humaniseKeys` (see the prop doc above) — a user-keyed
                option like `field_mapping` must render its keys verbatim.
                VALUES stay verbatim either way: they're the reader's own
                data (column names, prompt text), not ours to relabel. The
                raw key is kept recoverable via `title` whenever it's
                humanised (elspeth-b9ebdf9011 live-check fix, tightened to
                fail closed in round 3 after `field_mapping` was found
                relabeling reader column names). */}
            {humaniseKeys ? (
              <dt title={key}>{titleCaseLabel(key)}</dt>
            ) : (
              <dt>{key}</dt>
            )}
            <dd>
              <ConfigValue value={nestedValue} humaniseKeys={humaniseKeys} />
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
  structuralKeys,
}: {
  values: Record<string, unknown>;
  emptyText: string;
  /** Per-entry keys of `values` (as `values` actually keys them — a raw
   *  option key for a direct caller, or a display LABEL for a caller that
   *  pre-relabels, like OptionRows' advanced tier via `pick()`) whose value
   *  should have ITS OWN nested keys humanised. Omit entirely to render
   *  every entry's nested keys verbatim, unconditionally — this is
   *  GraphView's "Connections & schema" panel's contract, deliberately
   *  unchanged by elspeth-b9ebdf9011: it calls ConfigRows directly with raw
   *  wiring-field keys that were never in scope for this humanising. */
  structuralKeys?: ReadonlySet<string>;
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
            <ConfigValue value={value} humaniseKeys={structuralKeys?.has(key) ?? false} />
          </dd>
        </div>
      ))}
    </dl>
  );
}
