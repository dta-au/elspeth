import { useCallback, useDeferredValue, useEffect, useId, useLayoutEffect, useMemo, useState, type JSX } from "react";
import type { CompositionState } from "@/types";
import { optionBlobReferences, promptFileBindings, promptFileEditability, promptFileSlots, type PromptFileBinding } from "@/lib/blobBindings";
import { blobBannerMessages, useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { usePromptDraftStore } from "@/stores/promptDraftStore";
import { sortedSourceEntries, sourceComponentId } from "@/utils/compositionState";
import { BlobFileName } from "@/components/blobs/BlobFileName";
import { BreakableName } from "@/components/blobs/BreakableName";
import { AlertBanner, Button, Input } from "@/components/ui";
import { ErrorBoundary } from "@/components/common/ErrorBoundary";
import { NODE_LABELS } from "./GraphOutputs";
import { optionLabel, promptSlotLabel } from "./OptionRows";

interface GraphFilesProps {
  state: CompositionState;
  onShowNode: (componentId: string) => void;
  /** Ids drawn in the current graph view; a hidden component cannot be shown. */
  visibleNodeIds?: ReadonlySet<string>;
}

interface FileMapping {
  key: string;
  blobId: string;
  kind: string;
  component: string;
  /** The graph's node id for this component (sources use `sourceComponentId`). */
  componentId: string;
  /** Only an LLM step's prompt slots can be edited here. */
  isNode: boolean;
  /** `entry` is the rest of the path inside a multi-key option (e.g. `criteria.content` in `files.criteria.content`). */
  fields: Array<{ path: string; label: string; entry?: string }>;
}

/** Option keys that hold a file reference read as "File", never as "Blob Ref". */
const FILE_REFERENCE_OPTIONS: ReadonlySet<string> = new Set(["blob_ref", "blob_id"]);

function referenceLabel(option: string): string {
  return FILE_REFERENCE_OPTIONS.has(option) || option.endsWith("_blob_id") ? "File" : optionLabel(option);
}

/** One row per (component, file): every option field of that component that references the file. */
function fileMappings(state: CompositionState): FileMapping[] {
  const components = [
    ...sortedSourceEntries(state).map(([name, source]) => ({
      key: `source:${name}`, id: sourceComponentId(name), kind: "Source", name, options: source.options, isLlm: false, isNode: false,
    })),
    ...state.nodes.map((node) => ({
      key: `node:${node.id}`, id: node.id, kind: NODE_LABELS[node.node_type], name: node.id, options: node.options,
      isLlm: node.plugin === "llm", isNode: true,
    })),
    ...state.outputs.map((output) => ({
      key: `output:${output.name}`, id: output.name, kind: "Sink", name: output.name, options: output.options, isLlm: false, isNode: false,
    })),
  ];
  const mappings = new Map<string, FileMapping>();
  for (const component of components) {
    const promptLabels = new Map((component.isLlm ? promptFileSlots(component.options) : [])
      .map((slot) => [slot.fieldPath, promptSlotLabel(slot.fieldPath, slot.queryName)]));
    for (const reference of optionBlobReferences(component.options)) {
      const key = JSON.stringify([component.key, reference.blobId]);
      const mapping: FileMapping = mappings.get(key) ?? {
        key, blobId: reference.blobId, kind: component.kind, component: component.name, componentId: component.id,
        isNode: component.isNode, fields: [],
      };
      if (!mapping.fields.some((field) => field.path === reference.fieldPath)) {
        // Copy-register rule: the raw option path stays in the title; visible
        // text is the option's label, plus the rest of the path (as code) when
        // the option holds entries, so two entries never read the same.
        const prompt = promptLabels.get(reference.fieldPath);
        const [option, ...rest] = reference.fieldPath.split(".");
        mapping.fields.push(prompt !== undefined ? { path: reference.fieldPath, label: prompt }
          : { path: reference.fieldPath, label: referenceLabel(option), ...(rest.length > 0 ? { entry: rest.join(".") } : {}) });
      }
      mappings.set(key, mapping);
    }
  }
  return [...mappings.values()];
}

/**
 * Which graph component uses each session file, read from the graph's own
 * references; file contents never enter this table. Enumeration waits until
 * the section is opened, so a large graph costs nothing while it is closed.
 */
export function GraphFiles({ state, onShowNode, visibleNodeIds }: GraphFilesProps): JSX.Element {
  const [open, setOpen] = useState(false);
  const [count, setCount] = useState<{ state: CompositionState; value: number } | null>(null);
  const recordCount = useCallback((current: CompositionState, value: number) => {
    setCount((previous) => previous?.state === current && previous.value === value ? previous : { state: current, value });
  }, []);
  return <details className="graph-detail-table" onToggle={(event) => setOpen(event.currentTarget.open)}>
    {/* The count shows as a pill once known; the parentheses stay in the text
        (visually hidden), so AT and text locators read "Files (n)". */}
    <summary>
      Files{count?.state === state && <>
        <span className="visually-hidden"> (</span>
        <span className="graph-outputs-count">{count.value}</span>
        <span className="visually-hidden">)</span>
      </>}
    </summary>
    {open && <ErrorBoundary key={state.id} label="File mappings table">
      <FileMappings state={state} onShowNode={onShowNode} visibleNodeIds={visibleNodeIds} onCount={recordCount} />
    </ErrorBoundary>}
  </details>;
}

function FileMappings({ state, onShowNode, visibleNodeIds, onCount }: GraphFilesProps & {
  onCount: (state: CompositionState, count: number) => void;
}): JSX.Element {
  const rows = useMemo(() => fileMappings(state), [state]);
  const count = new Set(rows.map((row) => row.blobId)).size;
  useEffect(() => onCount(state, count), [state, count, onCount]);
  useLayoutEffect(() => {
    if (rows.length > 0 && useSessionStore.getState().activeSessionId === state.session_id) {
      void useBlobStore.getState().loadBlobs(state.session_id);
    }
  }, [rows.length, state.session_id]);
  const activeSessionId = useSessionStore((store) => store.activeSessionId);
  const currentStateId = useSessionStore((store) => store.compositionState?.id);
  const blobSessionId = useBlobStore((store) => store.activeSessionId);
  const blobs = useBlobStore((store) => store.blobs);
  const loading = useBlobStore((store) => store.isLoading);
  const error = useBlobStore((store) => store.activeSessionId === state.session_id ? store.error : null);
  const listError = useBlobStore((store) => store.activeSessionId === state.session_id ? store.listError : null);
  const listLoadFailed = useBlobStore((store) => store.activeSessionId === state.session_id && store.listLoadFailed);
  const bannerMessages = blobBannerMessages(error, listError);
  const pending = useInterpretationEventsStore((store) => store.pendingBySession[state.session_id]);
  const bindings = useMemo(() => promptFileBindings(state), [state]);
  const drafts = usePromptDraftStore((store) => store.drafts);
  const sameSession = activeSessionId === state.session_id;
  const [filter, setFilter] = useState("");
  const query = useDeferredValue(filter.trim().toLowerCase());
  const filenames = useMemo(() => new Map(sameSession && blobSessionId === state.session_id
    ? blobs.map((blob) => [blob.id.toLowerCase(), blob.filename]) : []), [sameSession, blobSessionId, state.session_id, blobs]);
  // Stable order: file name (the id when the name is unknown), then the
  // component's place in the graph (sources, steps, sinks as authored).
  const shown = useMemo(() => {
    const order = new Map(rows.map((row, index) => [row.key, index]));
    const name = (row: FileMapping) => filenames.get(row.blobId) ?? row.blobId;
    return rows
      .filter((row) => query === "" || name(row).toLowerCase().includes(query) || row.component.toLowerCase().includes(query))
      .sort((left, right) => name(left).localeCompare(name(right)) || (order.get(left.key) ?? 0) - (order.get(right.key) ?? 0));
  }, [rows, filenames, query]);
  const filterId = useId();
  // Slots of this graph that hold unsaved prompt text, as "<node>\u0000<field>".
  const draftSlots = useMemo(() => new Set(Object.values(drafts)
    .filter((draft) => draft.sessionId === state.session_id).map((draft) => `${draft.nodeId}\u0000${draft.fieldPath}`)), [drafts, state.session_id]);
  // The body is a size container: in a narrow pane each row stacks into a
  // block (name, then who uses it) instead of cutting columns off.
  return <div className="graph-files-body">
    {sameSession && bannerMessages.length > 0 && <AlertBanner className="graph-files-alert" action={listLoadFailed ? (
      // The same list read the table makes when it opens, issued again.
      <Button type="button" variant="ghost" compact onClick={() => void useBlobStore.getState().loadBlobs(state.session_id)}>Retry</Button>
    ) : null}>
      {bannerMessages.map((message) => <span key={message} style={{ display: "block" }}>{message}</span>)}
    </AlertBanner>}
    {rows.length === 0 ? <p className="graph-config-empty-value">No session files are attached to this graph.</p>
      : <>
      <div className="graph-files-filter">
        <label htmlFor={filterId}>Filter by file or step</label>
        <Input id={filterId} type="search" value={filter} onChange={(event) => setFilter(event.target.value)}
          autoComplete="off" spellCheck={false} aria-describedby={`${filterId}-count`} />
        <span id={`${filterId}-count`} className="graph-files-filter-count" role="status">
          {query === "" ? `${rows.length} ${rows.length === 1 ? "row" : "rows"}` : `${shown.length} of ${rows.length} rows`}
        </span>
      </div>
      <div className="graph-files-scroll" tabIndex={0} role="group" aria-label="File mappings table">
        {/* Explicit table roles: the narrow layout restyles these elements as
            blocks, and some browsers drop a table's semantics with its display. */}
        <table role="table">
          {/* Actions lead each row so an open step panel, which overlays the
              right of the workspace, never covers them. */}
          <thead role="rowgroup"><tr role="row">
            <th scope="col" role="columnheader">Actions</th><th scope="col" role="columnheader">File</th>
            <th scope="col" role="columnheader">Used by</th><th scope="col" role="columnheader">Fields</th>
          </tr></thead>
          <tbody role="rowgroup">{shown.map((row) => {
            const unsavedFields = row.isNode ? row.fields.filter((field) => draftSlots.has(`${row.componentId}\u0000${field.path}`)) : [];
            const rowBindings = row.isNode ? bindings.filter((item) => item.nodeId === row.componentId && item.blobId === row.blobId) : [];
            // Edit file opens the slot holding unsaved text first, so the user lands on it.
            const binding = rowBindings.find((item) => unsavedFields.some((field) => field.path === item.fieldPath)) ?? rowBindings[0];
            return <FileMappingRow key={row.key} row={row} state={state} onShowNode={onShowNode}
            hidden={visibleNodeIds !== undefined && !visibleNodeIds.has(row.componentId)}
            binding={binding} unsaved={unsavedFields.length > 0}
            sameSession={sameSession} current={sameSession && currentStateId === state.id}
            listReady={!loading && blobSessionId === state.session_id} listError={listError} retryOffered={listLoadFailed}
            blobs={blobSessionId === state.session_id ? blobs : []} pendingEvents={Object.values(pending ?? {})} />;
          })}
          </tbody>
        </table>
        {shown.length === 0 && <p className="graph-config-empty-value">No file or step matches this filter.</p>}
      </div>
      </>}
  </div>;
}

function FileMappingRow({ row, state, onShowNode, hidden, binding, unsaved, sameSession, current, listReady, listError, retryOffered, blobs, pendingEvents }: {
  row: FileMapping;
  /** A prompt slot in this row holds unsaved text in the editor. */
  unsaved: boolean;
  state: CompositionState;
  onShowNode: (componentId: string) => void;
  hidden: boolean;
  binding: PromptFileBinding | undefined;
  sameSession: boolean;
  current: boolean;
  listReady: boolean;
  listError: string | null;
  /** The banner above the table offers Retry (the list read failed). */
  retryOffered: boolean;
  blobs: ReturnType<typeof useBlobStore.getState>["blobs"];
  pendingEvents: Parameters<typeof promptFileEditability>[0]["pendingEvents"];
}): JSX.Element {
  const id = useId();
  const listIncomplete = listError !== null;
  // The prompt editor's own rules decide (as in the inspector's prompt rows
  // and the file manager): a refused save still opens the editor read-only
  // with its reason; only a file with no prompt text to show stays shut. The
  // reason rides on title + an sr-only description, as in the file list.
  const editability = binding && current && listReady && !listIncomplete
    ? promptFileEditability({ state, blobs, listIncomplete, pendingEvents, nodeId: binding.nodeId, fieldPath: binding.fieldPath, blobId: row.blobId })
    : null;
  const editReason = !current ? "Open the current graph to edit its files."
    : !listReady ? "Wait for the file list to load."
    : listIncomplete ? (retryOffered ? "File names are unavailable. Use Retry above." : "File names are unavailable.")
    : binding === undefined ? "Only prompt files can be edited here."
    : editability?.editable === false ? editability.reason : null;
  const editBlocked = binding === undefined || editability === null || (!editability.editable && !editability.opens);
  const showReason = hidden ? "This component is hidden by the current graph view." : null;
  return <tr role="row">
    <td role="cell">
      <div className="graph-file-actions">
        <Button type="button" compact aria-label={`Show node ${row.component}`}
          aria-disabled={hidden} title={showReason ?? undefined} aria-describedby={showReason ? `${id}-show` : undefined}
          onClick={() => { if (!hidden) onShowNode(row.componentId); }}>Show node</Button>
        {showReason && <span id={`${id}-show`} className="sr-only">{showReason}</span>}
        <Button type="button" compact aria-label={`Edit file for ${row.component}`}
          aria-disabled={editBlocked} title={editReason ?? undefined} aria-describedby={editReason ? `${id}-edit` : undefined}
          onClick={() => {
            if (editBlocked || binding === undefined) return;
            onShowNode(row.componentId);
            useBlobStore.getState().requestPromptEdit(binding.nodeId, binding.fieldPath);
          }}>Edit file</Button>
        {editReason && <span id={`${id}-edit`} className="sr-only">{editReason}</span>}
      </div>
    </td>
    <th scope="row" role="rowheader">{sameSession
      ? <BlobFileName bare blobId={row.blobId} fallback={listIncomplete ? "File name unavailable" : undefined} />
      : <span title={row.blobId}>File reference</span>}</th>
    <td role="cell">{row.kind}: <code><BreakableName name={row.component} /></code>{unsaved && <> <span className="prompt-editor-tag">Unsaved changes</span></>}</td>
    <td role="cell" className="graph-files-fields">{row.fields.map((field) => <div key={field.path} title={field.path}>
      {field.label}{field.entry !== undefined && <>: <code>{field.entry}</code></>}
    </div>)}</td>
  </tr>;
}
