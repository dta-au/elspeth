// ============================================================================
// OptionRows — the ONE renderer for a component's plugin options, shared by
// the graph node inspector and the Spec tab so the two views cannot drift
// (elspeth-a6ea581e8a, elspeth-b9ebdf9011).
//
// Three tiers, decided by key (Wave 1; catalog-tier-driven ordering is Wave 2):
//   1. ESSENTIAL_OPTION_KEYS — what the reader authored; always visible, in
//      this order.
//   2. everything else — "Advanced settings (N)" <details>, open only when the
//      show_advanced preference is on.
//   3. INTERNAL_OPTION_KEYS — wire bookkeeping (review event ids, blob refs,
//      content hashes). Never rendered as rows; reachable only through the
//      "Raw options (JSON)" block, which itself renders only with show_advanced.
// ============================================================================

import { CodeBlock } from "@/components/chat/CodeBlock";
import { titleCaseLabel } from "@/components/catalog/pluginDisplayName";
import { useShowAdvanced } from "@/stores/preferencesStore";

import { ConfigRows, ConfigValue } from "./ConfigRows";

// Wire sentinel for a blob-backed source's `path` knob (mirrors
// BLOB_REF_PATH_PREFIX in web/composer/guided/protocol.py and
// components/chat/guided/SchemaFormTurn.tsx's `maskBlobRef`): the guided
// emitter commits `blob:<blob_ref>` in place of the absolute storage_path,
// but a raw UUID means nothing to the reader and duplicates the decoy
// `blob_ref` internal key this component already hides. `path` is
// essential-tier (below), so it renders unconditionally — the raw sentinel
// must never reach visible text, only a `title` attribute (copy-register
// rule: identifiers go in title/data-* or a <code> inside a closed
// <details>; a <details> is not itself a firewall since its children still
// land in `textContent`) (elspeth-b9ebdf9011).
const BLOB_REF_PATH_PREFIX = "blob:";
const BLOB_REF_FRIENDLY_LABEL = "Uploaded sample data";

// Visible labels for the authored keys (copy-register rule: no snake_case in
// visible text). Anything not listed falls back to titleCaseLabel(key), the
// frontend's single title-casing implementation (elspeth-d2de348437).
export const OPTION_LABELS: Readonly<Record<string, string>> = {
  prompt_template: "Prompt",
  system_prompt: "System prompt",
  profile: "Model profile",
  model: "Model",
  response_field: "Answer written to",
  path: "File",
  schema: "Row schema",
  mode: "Mode",
  fields: "Fields",
  field_mapping: "Field mapping",
  select_only: "Keep only",
  columns: "Columns",
  url: "URL",
  query: "Query",
};

export function optionLabel(key: string): string {
  return OPTION_LABELS[key] ?? titleCaseLabel(key);
}

export const ESSENTIAL_OPTION_KEYS: readonly string[] = Object.keys(OPTION_LABELS);

export const INTERNAL_OPTION_KEYS: ReadonlySet<string> = new Set([
  "interpretation_requirements",
  "blob_ref",
  "source_authoring",
  "resolved_prompt_template_hash",
  "prompt_template_source",
  "lookup_source",
  "system_prompt_source",
]);

// Re-key by visible label. (The raw key is recoverable from the Raw options
// block; ConfigRows has no per-row title plumbing and none is added here.)
function pick(options: Record<string, unknown>, keys: readonly string[]): Record<string, unknown> {
  const out: Record<string, unknown> = {};
  for (const key of keys) {
    if (Object.prototype.hasOwnProperty.call(options, key)) out[optionLabel(key)] = options[key];
  }
  return out;
}

export function OptionRows({
  options,
  ariaLabel,
}: {
  options: Record<string, unknown>;
  ariaLabel: string;
}): JSX.Element {
  const showAdvanced = useShowAdvanced();
  const essential = pick(options, ESSENTIAL_OPTION_KEYS);
  const advancedKeys = Object.keys(options).filter(
    (key) => !ESSENTIAL_OPTION_KEYS.includes(key) && !INTERNAL_OPTION_KEYS.has(key),
  );
  const advanced = pick(options, advancedKeys);
  const isEmpty = Object.keys(essential).length === 0 && advancedKeys.length === 0;

  return (
    <div className="option-rows" role="region" aria-label={ariaLabel}>
      {isEmpty ? (
        <p className="graph-config-empty-value">No settings for this step.</p>
      ) : (
        <>
          {Object.keys(essential).length > 0 && (
            <dl className="graph-config-rows">
              {Object.entries(essential).map(([label, value]) => {
                const maskBlobPath =
                  label === optionLabel("path") &&
                  typeof value === "string" &&
                  value.startsWith(BLOB_REF_PATH_PREFIX);
                return (
                  <div key={label}>
                    <dt>{label}</dt>
                    <dd>
                      {maskBlobPath ? (
                        <span title={value as string}>{BLOB_REF_FRIENDLY_LABEL}</span>
                      ) : (
                        <ConfigValue value={value} />
                      )}
                    </dd>
                  </div>
                );
              })}
            </dl>
          )}
          {advancedKeys.length > 0 && (
            <details className="option-rows-advanced" open={showAdvanced}>
              <summary>Advanced settings ({advancedKeys.length})</summary>
              <ConfigRows values={advanced} emptyText="" />
            </details>
          )}
        </>
      )}
      {showAdvanced && (
        <details className="option-rows-raw">
          <summary>Raw options (JSON)</summary>
          <CodeBlock
            code={JSON.stringify(options, null, 2)}
            language="json"
            prettyJson
            showCopy={false}
          />
        </details>
      )}
    </div>
  );
}
