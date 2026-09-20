// ============================================================================
// OptionRows — the ONE renderer for a component's plugin options, shared by
// the graph node inspector and the Spec tab so the two views cannot drift
// (elspeth-a6ea581e8a, elspeth-b9ebdf9011).
//
// Two tiers when the catalog schema for this plugin is cached
// (usePluginCatalogStore), decided by the catalog's own field tiers
// (knob_schema.py _attach_tier): essential/common tiers render visibly, in
// schema field order; advanced-tier and schema-unknown keys go under
// "Advanced settings (N)" (open only when the show_advanced preference is
// on). When the schema is NOT yet cached (catalog never loaded, invalidated,
// structural node with no plugin), the render falls back to a static split:
//   1. FALLBACK_VISIBLE_OPTION_KEYS — a fixed allowlist, always visible, in
//      this order.
//   2. everything else — "Advanced settings (N)".
// In both cases:
//   3. INTERNAL_OPTION_KEYS — wire bookkeeping (review event ids, blob refs,
//      content hashes). Never rendered as rows; reachable only through the
//      "Raw options (JSON)" block, which itself renders only with show_advanced.
// ============================================================================

import { useEffect } from "react";

import { CodeBlock } from "@/components/chat/CodeBlock";
import { titleCaseLabel } from "@/components/catalog/pluginDisplayName";
import { useShowAdvanced } from "@/stores/preferencesStore";
import { usePluginCatalogStore } from "@/stores/pluginCatalogStore";

import { ConfigValue, STRUCTURAL_OPTION_CONTAINER_KEYS } from "./ConfigRows";

// Wire sentinel for a blob-backed source's `path` knob (mirrors
// BLOB_REF_PATH_PREFIX in web/composer/guided/protocol.py and
// components/chat/guided/SchemaFormTurn.tsx's `maskBlobRef`): the guided
// emitter commits `blob:<blob_ref>` in place of the absolute storage_path,
// but a raw UUID means nothing to the reader and duplicates the decoy
// `blob_ref` internal key this component already hides. `path` renders
// unconditionally in one partition or the other — the raw sentinel must
// never reach visible text, only a `title` attribute (copy-register rule:
// identifiers go in title/data-* or a <code> inside a closed <details>; a
// <details> is not itself a firewall since its children still land in
// `textContent`) (elspeth-b9ebdf9011). Masking is bound to the VALUE, not the
// partition, so it applies whether the catalog tiers `path` essential,
// common, advanced, or the schema is not cached at all.
const BLOB_REF_PATH_PREFIX = "blob:";
const BLOB_REF_FRIENDLY_LABEL = "Uploaded sample data";

// Visible labels for the authored keys (copy-register rule: no snake_case in
// visible text). Anything not listed falls back to titleCaseLabel(key), the
// frontend's single title-casing implementation (elspeth-d2de348437).
export const OPTION_LABELS: Readonly<Record<string, string>> = {
  prompt_template: "User prompt",
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

// Fallback partition when the plugin's catalog schema is not cached (catalog
// never loaded, invalidated, structural node). Declared explicitly — NOT
// derived from OPTION_LABELS — so a copy edit cannot change what is visible.
export const FALLBACK_VISIBLE_OPTION_KEYS: readonly string[] = [
  "prompt_template", "system_prompt", "profile", "model", "response_field", "path",
  "schema", "mode", "fields", "field_mapping", "select_only", "columns", "url", "query",
];

// STRUCTURAL_OPTION_CONTAINER_KEYS by LABEL, not raw key: both call sites
// below hand ConfigValue a value that has already been re-keyed by `pick()`.
// Computing this once here keeps that re-keying detail local to this module.
const STRUCTURAL_OPTION_CONTAINER_LABELS: ReadonlySet<string> = new Set(
  Array.from(STRUCTURAL_OPTION_CONTAINER_KEYS, optionLabel),
);

export const INTERNAL_OPTION_KEYS: ReadonlySet<string> = new Set([
  "interpretation_requirements",
  "blob_ref",
  "source_authoring",
  "approved_prompt_artifact_hash",
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

// Renders one row's value, masking the blob-path sentinel (above) in place of
// the generic ConfigValue walk. A plain `if` here lets TypeScript narrow
// `value` to `string` for the `title` attribute directly from the `typeof`
// check, with no cast. Shared by BOTH partitions (visible and advanced) so
// masking is a property of the value, never of which disclosure it lands in.
function OptionValue({ label, value }: { label: string; value: unknown }): JSX.Element {
  if (
    label === optionLabel("path") &&
    typeof value === "string" &&
    value.startsWith(BLOB_REF_PATH_PREFIX)
  ) {
    return <span title={value}>{BLOB_REF_FRIENDLY_LABEL}</span>;
  }
  return <ConfigValue value={value} humaniseKeys={STRUCTURAL_OPTION_CONTAINER_LABELS.has(label)} />;
}

function OptionDl({ rows }: { rows: Record<string, unknown> }): JSX.Element {
  return (
    <dl className="graph-config-rows">
      {Object.entries(rows).map(([label, value]) => (
        <div key={label}>
          <dt>{label}</dt>
          <dd><OptionValue label={label} value={value} /></dd>
        </div>
      ))}
    </dl>
  );
}

// Every llm step carries BOTH prompt roles, and the box always shows both,
// system first — present or not. The generic rows below are key-driven, so a
// step authored with a user prompt only used to render a lone prompt row with
// no sign a role was missing (session 60ab6a67). The not-set sentences are the
// reader-facing twin of Stage-1's llm_system_prompt_missing /
// llm_user_prompt_missing rejections.
const LLM_PROMPT_ROLE_KEYS: readonly string[] = ["system_prompt", "prompt_template"];
const SYSTEM_PROMPT_NOT_SET = "Not set. Every LLM step needs a system prompt.";
const USER_PROMPT_NOT_SET = "Not set. Every LLM step needs a user prompt.";

function isUnsuppliedPrompt(value: unknown): boolean {
  return value === undefined || value === null || (typeof value === "string" && value.trim() === "");
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

// A multi-query step has ONE system prompt and one user prompt PER QUERY. Both
// authoring forms the plugin accepts (a mapping keyed by query name, or a list
// of named entries) resolve to [name, entry] pairs; a malformed entry is
// skipped here and reported by validation, not by this box. Mirrors the
// backend's `_well_formed_query_entries`, positional label included.
function queryEntries(queries: unknown): Array<[string, Record<string, unknown>]> {
  if (Array.isArray(queries)) {
    return queries.flatMap((entry, index): Array<[string, Record<string, unknown>]> =>
      isRecord(entry) ? [[typeof entry.name === "string" && entry.name !== "" ? entry.name : `#${index}`, entry]] : [],
    );
  }
  if (isRecord(queries)) {
    return Object.entries(queries).flatMap(([name, entry]): Array<[string, Record<string, unknown>]> =>
      isRecord(entry) ? [[name, entry]] : [],
    );
  }
  return [];
}

function PromptRoleValue({ label, value, notSet }: { label: string; value: unknown; notSet: string }): JSX.Element {
  if (isUnsuppliedPrompt(value)) return <span className="option-rows-prompt-missing">{notSet}</span>;
  return <OptionValue label={label} value={value} />;
}

function LlmPromptRoles({ options }: { options: Record<string, unknown> }): JSX.Element {
  const systemLabel = optionLabel("system_prompt");
  const userLabel = optionLabel("prompt_template");
  const queries = queryEntries(options.queries);
  return (
    <dl className="graph-config-rows option-rows-prompt-roles">
      <div>
        <dt>{systemLabel}</dt>
        <dd><PromptRoleValue label={systemLabel} value={options.system_prompt} notSet={SYSTEM_PROMPT_NOT_SET} /></dd>
      </div>
      {queries.length === 0 ? (
        <div>
          <dt>{userLabel}</dt>
          <dd><PromptRoleValue label={userLabel} value={options.prompt_template} notSet={USER_PROMPT_NOT_SET} /></dd>
        </div>
      ) : (
        // The user prompt a query actually sends: its own template, else the
        // node-level fallback — the same resolution the runtime applies. The
        // query name is the reader's own identifier, so it is shown verbatim
        // in the value cell, never humanised into the label.
        queries.map(([name, entry]) => (
          <div key={name}>
            <dt>{userLabel}</dt>
            <dd>
              <span className="option-rows-query-name">{name}</span>
              <PromptRoleValue
                label={userLabel}
                value={isUnsuppliedPrompt(entry.template) ? options.prompt_template : entry.template}
                notSet={USER_PROMPT_NOT_SET}
              />
            </dd>
          </div>
        ))
      )}
    </dl>
  );
}

export function OptionRows({
  options,
  ariaLabel,
  plugin = null,
}: {
  options: Record<string, unknown>;
  ariaLabel: string;
  plugin?: { kind: "source" | "transform" | "sink"; name: string } | null;
}): JSX.Element {
  const showAdvanced = useShowAdvanced();
  const catalogKey = usePluginCatalogStore((s) => s.key);
  const schema = usePluginCatalogStore((s) =>
    plugin === null ? undefined : s.schemas[`${plugin.kind}:${plugin.name}`],
  );
  const loadSchema = usePluginCatalogStore((s) => s.loadSchema);
  const pluginKind = plugin?.kind;
  const pluginName = plugin?.name;
  useEffect(() => {
    // loadSchema no-ops on cached/loading keys and makes NO request while the
    // catalog has no key. Subscribing to `key` re-fires this on first catalog
    // load and after invalidate() (which wipes `schemas` and `key` together),
    // so a mounted inspector recovers instead of silently living on the
    // fallback partition.
    if (pluginKind !== undefined && pluginName !== undefined && catalogKey !== null) {
      void loadSchema(pluginKind, pluginName);
    }
  }, [pluginKind, pluginName, catalogKey, loadSchema]);

  const isLlmStep = plugin !== null && plugin.kind === "transform" && plugin.name === "llm";
  const candidateKeys = Object.keys(options).filter(
    (key) => !INTERNAL_OPTION_KEYS.has(key) && !(isLlmStep && LLM_PROMPT_ROLE_KEYS.includes(key)),
  );
  let visibleKeys: string[];
  let advancedKeys: string[];
  if (schema === undefined) {
    visibleKeys = FALLBACK_VISIBLE_OPTION_KEYS.filter((key) => candidateKeys.includes(key));
    advancedKeys = candidateKeys.filter((key) => !FALLBACK_VISIBLE_OPTION_KEYS.includes(key));
  } else {
    // A discriminated schema repeats same-named fields once per variant. Only
    // the active copy participates in tiering, using SchemaFormTurn's exact
    // visible_when predicate semantics against the authored options. Otherwise
    // registry order would decide the tier through a last-wins Map.
    //
    // Absent tier = "common": a field the catalog KNOWS but does not tier is
    // visible, never demoted. The operator-profile policy views hand-build
    // their projections and have shipped fields with no `tier`; this keeps the
    // same posture as `optionTier` in components/chat/guided/optionTiers.ts
    // (elspeth-a6ea581e8a). A key no active schema field lists remains advanced.
    const activeFields = schema.knob_schema.fields.filter(
      (field) =>
        field.visible_when === undefined
        || options[field.visible_when.field] === field.visible_when.equals,
    );
    const presentFields = activeFields.filter((field) => candidateKeys.includes(field.name));
    const essentials = presentFields
      .filter((field) => (field.tier ?? "common") === "essential")
      .map((field) => field.name);
    const commons = presentFields
      .filter((field) => (field.tier ?? "common") === "common")
      .map((field) => field.name);
    visibleKeys = [...essentials, ...commons];
    advancedKeys = candidateKeys.filter((key) => !visibleKeys.includes(key));
  }
  const visible = pick(options, visibleKeys);
  const advanced = pick(options, advancedKeys);
  const isEmpty = !isLlmStep && Object.keys(visible).length === 0 && advancedKeys.length === 0;

  return (
    <div className="option-rows" role="region" aria-label={ariaLabel}>
      {isEmpty ? (
        <p className="graph-config-empty-value">No settings for this step.</p>
      ) : (
        <>
          {isLlmStep && <LlmPromptRoles options={options} />}
          {Object.keys(visible).length > 0 && <OptionDl rows={visible} />}
          {advancedKeys.length > 0 && (
            <details className="option-rows-advanced" open={showAdvanced}>
              <summary>Advanced settings ({advancedKeys.length})</summary>
              <OptionDl rows={advanced} />
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
