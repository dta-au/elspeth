import { useEffect, useId, useRef, useState } from "react";
import { YamlView } from "@/components/inspector/YamlView";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { OPEN_YAML_MODAL_EVENT } from "@/lib/composer-events";
import { useSessionStore } from "@/stores/sessionStore";

export function ExportYamlModal(): JSX.Element | null {
  const [isOpen, setIsOpen] = useState(false);
  const dialogRef = useRef<HTMLDivElement>(null);
  const titleId = useId();
  // Set by YamlView from fetchYaml's source_blob_ids; null when this export
  // has no blob-backed source.
  //
  // Scoped to the active session before it is trusted. YamlView clears `yaml`
  // and `yamlError` before each fetch but leaves the binding standing, and its
  // error path never touches it — so between opening the modal and the fetch
  // resolving (or forever, if the fetch 409s on validation errors) the binding
  // can still describe a PREVIOUS export. Warning "source data is session
  // bound" off a stale signal would be exactly the unsubstantiated claim the
  // landscape wording below refuses to make.
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const storedBinding = useSessionStore((s) => s.exportedYamlBlobBinding);
  const blobBinding =
    storedBinding !== null && storedBinding.sessionId === activeSessionId
      ? storedBinding
      : null;

  useFocusTrap(dialogRef, isOpen, ".yaml-modal-close");

  useEffect(() => {
    function handleOpen() {
      setIsOpen(true);
    }

    window.addEventListener(OPEN_YAML_MODAL_EVENT, handleOpen);
    return () => window.removeEventListener(OPEN_YAML_MODAL_EVENT, handleOpen);
  }, []);

  useEffect(() => {
    if (!isOpen) return;

    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") {
        e.preventDefault();
        setIsOpen(false);
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [isOpen]);

  if (!isOpen) return null;

  return (
    <>
      <div
        className="yaml-modal-backdrop"
        data-testid="yaml-modal-backdrop"
        onClick={() => setIsOpen(false)}
        aria-hidden="true"
      />
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="yaml-modal"
      >
        <header className="yaml-modal-header">
          <h2 id={titleId}>Export YAML</h2>
          <button
            type="button"
            className="yaml-modal-close"
            onClick={() => setIsOpen(false)}
            aria-label="Close export YAML"
          >
            ×
          </button>
        </header>
        <div className="yaml-modal-body">
          {/* elspeth-8d17056117: the export used to leave silently, so a
              reader could mistake it for a standalone-runnable pipeline.
              Two omissions, deliberately worded from different evidence.

              The scope note is unconditional because it is true of every
              export: `landscape:` is stripped by design (yaml_generator.py
              — the audit URL comes from WebSettings at execution time,
              security fix S1), and no signal exists anywhere that an
              IMPORTED landscape block was dropped — yaml_importer never
              reads the key. Claiming "your landscape block was removed"
              would be asserting something the code cannot substantiate.

              The blob line IS conditional, because a real signal backs it:
              fetchYaml returns source_blob_ids and YamlView stores it as
              exportedYamlBlobBinding. */}
          <div className="yaml-modal-note" data-testid="yaml-export-scope-note">
            This is the pipeline definition only. Deployment-owned
            configuration — including the <code>landscape</code> audit
            destination — is supplied by the environment at run time and is
            never written here.
          </div>
          {blobBinding !== null && (
            <div
              className="yaml-modal-note yaml-modal-note--warn"
              data-testid="yaml-export-blob-note"
            >
              Source data is session-bound: this pipeline reads an uploaded
              file held in this session, so the path is not in the YAML. To
              run it elsewhere, bind an uploaded file on import.
            </div>
          )}
          <YamlView />
        </div>
      </div>
    </>
  );
}
