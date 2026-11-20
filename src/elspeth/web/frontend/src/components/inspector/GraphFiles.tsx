import { useCallback, useEffect, useId, useLayoutEffect, useMemo, useState, type JSX } from "react";
import type { CompositionState } from "@/types";
import { optionBlobReferences, promptFileBindings, promptFileEditability, promptFileSlots, type PromptFileBinding } from "@/lib/blobBindings";
import { useBlobStore } from "@/stores/blobStore";
import { useSessionStore } from "@/stores/sessionStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { sortedSourceEntries, sourceComponentId } from "@/utils/compositionState";
import { BlobFileName } from "@/components/blobs/BlobFileName";
import { Button } from "@/components/ui";
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
          : { path: reference.fieldPath, label: optionLabel(option), ...(rest.length > 0 ? { entry: rest.join(".") } : {}) });
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
  const loadError = useBlobStore((store) => store.activeSessionId === state.session_id ? store.error : null);
  const pending = useInterpretationEventsStore((store) => store.pendingBySession[state.session_id]);
  const bindings = useMemo(() => promptFileBindings(state), [state]);
  const sameSession = activeSessionId === state.session_id;
  return <>
    {sameSession && loadError && <p role="alert">{loadError}</p>}
    {rows.length === 0 ? <p className="graph-config-empty-value">No session files are attached to this graph.</p>
      : <div className="graph-files-scroll" tabIndex={0} role="group" aria-label="File mappings table">
        <table>
          <thead><tr><th scope="col">File</th><th scope="col">Used by</th><th scope="col">Fields</th><th scope="col">Actions</th></tr></thead>
          <tbody>{rows.map((row) => <FileMappingRow key={row.key} row={row} state={state} onShowNode={onShowNode}
            hidden={visibleNodeIds !== undefined && !visibleNodeIds.has(row.componentId)}
            binding={row.isNode ? bindings.find((item) => item.nodeId === row.componentId && item.blobId === row.blobId) : undefined}
            sameSession={sameSession} current={sameSession && currentStateId === state.id}
            listReady={!loading && blobSessionId === state.session_id} loadError={loadError}
            blobs={blobSessionId === state.session_id ? blobs : []} pendingEvents={Object.values(pending ?? {})} />)}
          </tbody>
        </table>
      </div>}
  </>;
}

function FileMappingRow({ row, state, onShowNode, hidden, binding, sameSession, current, listReady, loadError, blobs, pendingEvents }: {
  row: FileMapping;
  state: CompositionState;
  onShowNode: (componentId: string) => void;
  hidden: boolean;
  binding: PromptFileBinding | undefined;
  sameSession: boolean;
  current: boolean;
  listReady: boolean;
  loadError: string | null;
  blobs: ReturnType<typeof useBlobStore.getState>["blobs"];
  pendingEvents: Parameters<typeof promptFileEditability>[0]["pendingEvents"];
}): JSX.Element {
  const id = useId();
  // The prompt editor's own rules decide (as in the inspector's prompt rows
  // and the file manager): a refused save still opens the editor read-only
  // with its reason; only a file with no prompt text to show stays shut. The
  // reason rides on title + an sr-only description, as in the file list.
  const editability = binding && current && listReady && !loadError
    ? promptFileEditability({ state, blobs, pendingEvents, nodeId: binding.nodeId, fieldPath: binding.fieldPath, blobId: row.blobId })
    : null;
  const editReason = !current ? "Open the current graph to edit its files."
    : !listReady ? "Wait for the file list to load."
    : loadError ? "File names are unavailable. Reload the file list."
    : binding === undefined ? "Only prompt files can be edited here."
    : editability?.editable === false ? editability.reason : null;
  const editBlocked = binding === undefined || editability === null || (!editability.editable && !editability.opens);
  const showReason = hidden ? "This component is hidden by the current graph view." : null;
  return <tr>
    <th scope="row">{sameSession
      ? <BlobFileName blobId={row.blobId} fallback={loadError ? "File name unavailable" : undefined} />
      : <span title={row.blobId}>File reference</span>}</th>
    <td>{row.kind}: <code>{row.component}</code></td>
    <td>{row.fields.map((field) => <div key={field.path} title={field.path}>
      {field.label}{field.entry !== undefined && <>: <code>{field.entry}</code></>}
    </div>)}</td>
    <td>
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
  </tr>;
}
