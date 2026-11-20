import { useId, useMemo, useRef, useState } from "react";
import type { BlobMetadata, CompositionState } from "@/types/api";
import { Button, Input } from "@/components/ui";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { inlineContentMarker, promptFileSlots } from "@/lib/blobBindings";
import { promptSlotLabel } from "@/components/inspector/OptionRows";

interface FileWiringPadProps {
  blob: BlobMetadata;
  state: CompositionState | null;
  /** The active session's file list; only used to name the file a slot already reads. */
  blobs: readonly BlobMetadata[];
  /** Why connecting is refused right now (session loading, Composer busy, file or graph changed); null when allowed. */
  disabledReason: string | null;
  /** Set when Composer did not take the last request; the pad stays open with the user's input intact. */
  sendRefusal?: { message: string; seq: number } | null;
  /** Receives the user's chat request. The pad never writes the graph itself. */
  onConnect: (request: string) => void;
  onClose: () => void;
}

interface WiringSlot {
  /** Canonical `node:<id>.options.<field>` path the wiring tool accepts. */
  path: string;
  label: string;
  current: string;
  alreadyConnected: boolean;
}

const FILE_WIRING_INSTRUCTIONS_MAX_LENGTH = 4000;

function wiringSlots(state: CompositionState | null, blob: BlobMetadata, blobs: readonly BlobMetadata[]): WiringSlot[] {
  const blobId = blob.id.toLowerCase();
  return (state?.nodes ?? []).filter((node) => node.plugin === "llm").flatMap((node) => {
    const slots = promptFileSlots(node.options);
    // A step without a system prompt can still take one from a file.
    const withSystem = slots.some((slot) => slot.fieldPath === "system_prompt")
      ? slots : [...slots, { fieldPath: "system_prompt", value: undefined }];
    return withSystem.map((slot): WiringSlot => {
      const marker = inlineContentMarker(slot.value);
      const markerFile = marker ? blobs.find((file) => file.id.toLowerCase() === marker.blobId.toLowerCase()) : undefined;
      return {
        path: `node:${node.id}.options.${slot.fieldPath}`,
        label: `${node.id} — ${promptSlotLabel(slot.fieldPath, slot.queryName)}`,
        current: marker ? markerFile?.filename ?? "Missing file"
          : slot.value === undefined || slot.value === null ? "Not connected" : "Inline prompt",
        // Only the same file at its current hash counts as connected; a stale pin can be re-pointed.
        alreadyConnected: marker !== null && marker.blobId.toLowerCase() === blobId && marker.sha256 === blob.content_hash,
      };
    });
  });
}

/**
 * Lets the file's owner say where it should be used. The result is an
 * ordinary chat request: Composer chooses and runs the audited wiring tools,
 * and their review cards and refusals apply as for any typed request.
 */
export function FileWiringPad({ blob, state, blobs, disabledReason, sendRefusal = null, onConnect, onClose }: FileWiringPadProps) {
  const id = useId();
  const dialogRef = useRef<HTMLDivElement>(null);
  const [mode, setMode] = useState<"select" | "ai">("select");
  const [selected, setSelected] = useState<ReadonlySet<string>>(new Set());
  const [instructions, setInstructions] = useState("");
  useFocusTrap(dialogRef);

  const slots = useMemo(() => wiringSlots(state, blob, blobs), [state, blob, blobs]);
  const destinations = slots.filter((slot) => selected.has(slot.path) && !slot.alreadyConnected).map((slot) => slot.path);
  const canConnect = disabledReason === null && (mode === "select" ? destinations.length > 0 : instructions.trim().length > 0);

  const toggle = (path: string, checked: boolean) => {
    setSelected((previous) => {
      const next = new Set(previous);
      if (checked) next.add(path);
      else next.delete(path);
      return next;
    });
  };

  const connect = () => {
    if (!canConnect) return;
    const file = JSON.stringify({ blob_id: blob.id, filename: blob.filename });
    const task = mode === "select"
      ? `Connect this session file ${file} as the whole UTF-8 prompt at these selected slots using wire_blob_inline_ref: ${JSON.stringify(destinations)}. Replace the existing prompts only at those slots.`
      : `Connect this session file ${file} to prompts as requested: ${instructions.trim()}`;
    onConnect(`${task}\nKeep file references in the graph; use the file's exact contents without pasting or rewriting them. Keep all other graph and file content unchanged.`);
  };

  return <>
    <div className="confirm-dialog-backdrop" role="presentation" onClick={onClose} />
    {/* tabIndex -1: a click on the pad's text focuses the pad itself rather
        than the page body, so Escape still reaches this handler. It stays
        out of the Tab order (the focus trap skips tabindex="-1"). */}
    <div ref={dialogRef} className="confirm-dialog file-wiring-pad" role="dialog" aria-modal="true" tabIndex={-1}
      aria-labelledby={`${id}-title`} aria-describedby={`${id}-description`}
      onKeyDown={(event) => {
        // Stop here so the file manager's own Escape handler does not also close the manager.
        if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); onClose(); }
      }}>
      <header className="confirm-dialog-header">
        <h2 id={`${id}-title`} className="confirm-dialog-title">File wiring pad</h2>
      </header>
      <div className="confirm-dialog-body">
        <p id={`${id}-description`} className="confirm-dialog-message">
          Connect {blob.filename} to prompts. Choose the connections yourself or ask AI to choose them.
          Composer applies the connections as a chat request.
        </p>
        <fieldset className="file-wiring-mode">
          <legend>How to connect</legend>
          <label><Input type="radio" name={`${id}-mode`} checked={mode === "select"} onChange={() => setMode("select")} /> Choose prompts</label>
          <label><Input type="radio" name={`${id}-mode`} checked={mode === "ai"} onChange={() => setMode("ai")} /> Ask AI</label>
        </fieldset>
        {mode === "select" ? <fieldset className="file-wiring-targets">
          <legend>Prompt connections</legend>
          {slots.length === 0 && <p>No prompt slots yet. Ask AI where to connect this file.</p>}
          {slots.map((slot) => <label key={slot.path} className="file-wiring-target">
            <Input type="checkbox" checked={slot.alreadyConnected || selected.has(slot.path)} disabled={slot.alreadyConnected}
              onChange={(event) => toggle(slot.path, event.target.checked)} />
            <span>{slot.label}<small>{slot.alreadyConnected ? "Already connected" : slot.current}</small></span>
          </label>)}
        </fieldset> : <label className="file-wiring-instructions" htmlFor={`${id}-instructions`}>
          What should AI connect?
          <textarea className="input" id={`${id}-instructions`} rows={4} maxLength={FILE_WIRING_INSTRUCTIONS_MAX_LENGTH}
            value={instructions} onChange={(event) => setInstructions(event.target.value)} />
        </label>}
        {disabledReason && <p role="status">{disabledReason}</p>}
        {/* Keyed by seq: a new node per refusal, so assistive tech announces a repeat. */}
        {sendRefusal && disabledReason === null && <p key={sendRefusal.seq} role="alert">{sendRefusal.message}</p>}
      </div>
      <footer className="confirm-dialog-actions">
        <Button className="confirm-dialog-btn" onClick={onClose}>Cancel</Button>
        <Button variant="primary" className="confirm-dialog-btn" disabled={!canConnect} onClick={connect}>
          {mode === "select" ? "Connect selected prompts" : "Ask AI to connect"}
        </Button>
      </footer>
    </div>
  </>;
}
