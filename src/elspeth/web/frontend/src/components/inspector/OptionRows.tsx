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

import { type ComponentType, type JSX, useEffect, useId } from "react";

import { CodeBlock } from "@/components/chat/CodeBlock";
import { BlobFileName } from "@/components/blobs/BlobFileName";
import { useBlobFilename } from "@/components/blobs/BlobFileName";
import { Button } from "@/components/ui";
import { promptFileEditability, promptFileSlots, promptQueryCopyName, promptQueryEntries, promptQueryTemplatePath } from "@/lib/blobBindings";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { titleCaseLabel } from "@/components/catalog/pluginDisplayName";
import { useShowAdvanced } from "@/stores/preferencesStore";
import { usePluginCatalogStore } from "@/stores/pluginCatalogStore";

import { ConfigValue, STRUCTURAL_OPTION_CONTAINER_KEYS } from "./ConfigRows";
import { PromptCopyButton } from "./PromptCopyButton";

// Wire sentinel for a blob-backed source's `path` knob. The authoring
// surface commits `blob:<blob_ref>` in place of the absolute storage_path,
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
    return <BlobFileName blobId={value.slice(BLOB_REF_PATH_PREFIX.length)} fallback={BLOB_REF_FRIENDLY_LABEL} />;
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

function PromptRoleValue({ label, value, notSet, copyLabel }: { label: string; value: unknown; notSet: string; copyLabel: string }): JSX.Element {
  if (isUnsuppliedPrompt(value)) return <span className="option-rows-prompt-missing">{notSet}</span>;
  return <><OptionValue label={label} value={value} />{typeof value === "string" && <PromptCopyButton value={value} label={copyLabel} />}</>;
}

function PromptSlotActions({ value, nodeId, fieldPath, label, showInFiles }: {
  value: unknown; nodeId: string; fieldPath: string; label: string; showInFiles: boolean;
}): JSX.Element | null {
  const sessionId = useSessionStore((state) => state.activeSessionId);
  const compositionState = useSessionStore((state) => state.compositionState);
  const blobs = useBlobStore((state) => state.blobs);
  const blobSessionId = useBlobStore((state) => state.activeSessionId);
  const isLoading = useBlobStore((state) => state.isLoading);
  const pending = useInterpretationEventsStore((state) => sessionId ? state.pendingBySession[sessionId] : undefined);
  // Show in files needs a mounted file manager to reveal into: a host
  // registers itself (ChatPanel, wherever it can render BlobManager). The
  // surface can also opt out (the modal graph, whose focus trap hides it).
  const fileManagerReachable = useBlobStore((state) => state.fileManagerHosts > 0);
  if (!isRecord(value) || value.mode !== "inline_content" || typeof value.blob_ref !== "string" || sessionId === null) return null;
  return <BoundPromptActions blobId={value.blob_ref} nodeId={nodeId} fieldPath={fieldPath} label={label}
    sessionId={sessionId} state={compositionState} blobs={blobSessionId === sessionId ? blobs : []}
    loading={blobSessionId !== sessionId || isLoading} pendingEvents={Object.values(pending ?? {})}
    showInFiles={showInFiles && fileManagerReachable} />;
}

function BoundPromptActions({ blobId, nodeId, fieldPath, label, sessionId, state, blobs, loading, pendingEvents, showInFiles }: {
  blobId: string; nodeId: string; fieldPath: string; label: string; sessionId: string; showInFiles: boolean;
  state: ReturnType<typeof useSessionStore.getState>["compositionState"];
  blobs: ReturnType<typeof useBlobStore.getState>["blobs"];
  loading: boolean;
  pendingEvents: ReturnType<typeof useInterpretationEventsStore.getState>["resolvedBySession"][string];
}): JSX.Element {
  const filename = useBlobFilename(blobId) ?? (loading ? "Loading file name…" : "File not in this session");
  const present = blobs.some((blob) => blob.id.toLowerCase() === blobId.toLowerCase());
  const editability = promptFileEditability({ state, blobs, pendingEvents, nodeId, fieldPath, blobId });
  const checking = loading && !present;
  const editReason = checking ? "Checking files in this session." : editability.editable ? null : editability.reason;
  // Edit is a no-op only when there is no prompt text to show (file missing,
  // still processing, not text). Every other refusal opens the editor
  // read-only: Save is disabled there and the same reason is shown.
  const editBlocked = checking || (!editability.editable && !editability.opens);
  // useId, not the slot path: two query rows that fall back to the node-level
  // prompt share one slot path and would otherwise share one DOM id.
  const rowId = useId();
  const reasonId = `${rowId}-reason`;
  const missingReasonId = `${rowId}-missing`;
  return <span className="prompt-slot-actions">
    {showInFiles && <>
      {/* The accessible name STARTS with the visible label (WCAG 2.5.3), so
          "click Show in files" matches for voice-control users. */}
      <Button type="button" variant="ghost" compact aria-label={`Show in files: ${filename} (${label})`}
        aria-disabled={!present} aria-describedby={!present ? missingReasonId : undefined}
        onClick={(event) => { if (present) useBlobStore.getState().revealBlob(sessionId, blobId, filename, event.currentTarget); }}>
        Show in files
      </Button>
      {!present && <span id={missingReasonId} className="prompt-slot-reason">{loading ? "Checking files in this session." : "This file is not in this session."}</span>}
    </>}
    {/* data-prompt-edit-slot: the editor returns focus here when it closes. */}
    <Button type="button" variant="ghost" compact aria-label={`Edit ${filename} (${label})`}
      data-prompt-edit-slot={`${nodeId}:${fieldPath}`}
      aria-disabled={editBlocked} aria-describedby={editReason !== null ? reasonId : undefined}
      onClick={() => { if (!editBlocked) useBlobStore.getState().requestPromptEdit(nodeId, fieldPath); }}>
      Edit
    </Button>
    {editReason !== null && <span id={reasonId} className="prompt-slot-reason">{editReason}</span>}
  </span>;
}

/** The prompt file editor, as OptionRows mounts it inside a prompt's row. */
export type PromptSlotEditor = ComponentType<{ nodeId: string; blobId: string; fieldPath: string; queryName?: string }>;

export interface PromptSlotActionsConfig {
  nodeId: string;
  showInFiles?: boolean;
  /** Passed in by the host (not imported here: the editor imports this module). */
  Editor?: PromptSlotEditor;
}

function LlmPromptRoles({ options, slotActions }: { options: Record<string, unknown>; slotActions?: PromptSlotActionsConfig }): JSX.Element {
  const systemLabel = optionLabel("system_prompt");
  const userLabel = optionLabel("prompt_template");
  // A multi-query step has ONE system prompt and one user prompt PER QUERY.
  // Both authoring forms (a mapping keyed by query name, or a list of named
  // entries) come from promptQueryEntries, the same authority the editor and
  // the binding check use: `selector` builds the slot path (the list INDEX for
  // list form, as the server keys it), `name` is only ever a label. A
  // malformed entry is skipped here and reported by validation.
  const queries = promptQueryEntries(options.queries);
  // The user prompt a query actually sends: its own template, else the
  // node-level fallback — the same resolution the runtime applies.
  // copyName carries the list position, so copy labels stay unique when names repeat.
  const queryRows = queries.map((query) => {
    const { selector, name, entry } = query;
    const copyName = promptQueryCopyName(query);
    return isUnsuppliedPrompt(entry.template)
      ? { selector, name, copyName, value: options.prompt_template, fieldPath: "prompt_template" }
      : { selector, name, copyName, value: entry.template, fieldPath: promptQueryTemplatePath(selector) };
  });
  // One editor per file-backed slot (promptFileSlots, the editor's and the
  // server's slot authority), mounted in the row that shows that prompt.
  const Editor = slotActions?.Editor;
  const fileSlots = Editor === undefined ? [] : promptFileSlots(options).flatMap(({ fieldPath, value, queryName }) =>
    isRecord(value) && value.mode === "inline_content" && typeof value.blob_ref === "string"
      ? [{ fieldPath, blobId: value.blob_ref, queryName }] : []);
  const editorFor = (slot: (typeof fileSlots)[number] | undefined): JSX.Element | null => Editor !== undefined && slotActions !== undefined && slot !== undefined
    ? <Editor key={`${slotActions.nodeId}:${slot.fieldPath}:${slot.blobId}`} nodeId={slotActions.nodeId} {...slot} />
    : null;
  const editorAt = (fieldPath: string): JSX.Element | null => editorFor(fileSlots.find((slot) => slot.fieldPath === fieldPath));
  // Query rows that fall back to the step-level prompt share ONE slot, so one
  // editor: it sits under the first such row and the others say so.
  const sharedHost = queryRows.find((row) => row.fieldPath === "prompt_template");
  const sharedIsFile = fileSlots.some((slot) => slot.fieldPath === "prompt_template");
  // A file-backed slot no row shows (a step-level prompt file every query
  // overrides) can still be opened from the file list; its editor goes last.
  const rowPaths = new Set(["system_prompt", ...(queries.length === 0 ? ["prompt_template"] : queryRows.map((row) => row.fieldPath))]);
  const unshown = fileSlots.filter((slot) => !rowPaths.has(slot.fieldPath));
  return (<>
    <dl className="graph-config-rows option-rows-prompt-roles">
      <div>
        <dt>{systemLabel}</dt>
        <dd><PromptRoleValue label={systemLabel} value={options.system_prompt} notSet={SYSTEM_PROMPT_NOT_SET} copyLabel="system prompt" />
          {slotActions && <PromptSlotActions value={options.system_prompt} nodeId={slotActions.nodeId} fieldPath="system_prompt" label={systemLabel} showInFiles={slotActions.showInFiles ?? true} />}
          {editorAt("system_prompt")}</dd>
      </div>
      {queries.length === 0 ? (
        <div>
          <dt>{userLabel}</dt>
          <dd><PromptRoleValue label={userLabel} value={options.prompt_template} notSet={USER_PROMPT_NOT_SET} copyLabel="user prompt template" />
            {slotActions && <PromptSlotActions value={options.prompt_template} nodeId={slotActions.nodeId} fieldPath="prompt_template" label={userLabel} showInFiles={slotActions.showInFiles ?? true} />}
            {editorAt("prompt_template")}</dd>
        </div>
      ) : (
        // The query name is the reader's own identifier, so it is shown
        // verbatim in the value cell, never humanised into the label.
        queryRows.map((row) => (
          <div key={row.selector}>
            <dt>{userLabel}</dt>
            <dd>
              <span className="option-rows-query-name">{row.name}</span>
              <PromptRoleValue label={userLabel} value={row.value} notSet={USER_PROMPT_NOT_SET}
                copyLabel={`prompt template for query ${row.copyName}`} />
              {slotActions && <PromptSlotActions value={row.value} nodeId={slotActions.nodeId} fieldPath={row.fieldPath}
                label={`${userLabel}, query ${row.name}`} showInFiles={slotActions.showInFiles ?? true} />}
              {row.fieldPath !== "prompt_template" || row === sharedHost
                ? editorAt(row.fieldPath)
                : sharedIsFile && sharedHost !== undefined && (
                  <span className="prompt-slot-shared">
                    This query uses the step&apos;s shared prompt file. Its editor opens under query {sharedHost.name}.
                  </span>
                )}
            </dd>
          </div>
        ))
      )}
    </dl>
    {unshown.map(editorFor)}
  </>);
}

export function OptionRows({
  options,
  ariaLabel,
  plugin = null,
  slotActions,
}: {
  options: Record<string, unknown>;
  ariaLabel: string;
  plugin?: { kind: "source" | "transform" | "sink"; name: string } | null;
  slotActions?: PromptSlotActionsConfig;
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
    // the active copy participates in tiering, using the catalog's
    // visible_when predicate semantics against the authored options. Otherwise
    // registry order would decide the tier through a last-wins Map.
    //
    // Absent tier = "common": a field the catalog KNOWS but does not tier is
    // visible, never demoted. The operator-profile policy views hand-build
    // their projections and have shipped fields with no `tier`; this keeps the
    // field's default tier. A key no active schema field lists remains advanced.
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
          {isLlmStep && <LlmPromptRoles options={options} slotActions={slotActions} />}
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
