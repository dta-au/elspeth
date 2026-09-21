import { type JSX, useEffect, useRef, useState } from "react";
import { Button, Input } from "@/components/ui";
import * as library from "@/api/library";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { useAuthStore } from "@/stores/authStore";
import { useMailboxStore } from "@/stores/mailboxStore";
import { useSessionStore } from "@/stores/sessionStore";
import type { LibraryCuratorAction, LibraryEntry, LibraryView } from "@/types/library";
import "./library.css";

const TABS: readonly LibraryView[] = ["accepted", "mine", "queue"];
const TAB_LABELS: Record<LibraryView, string> = {
  accepted: "Browse",
  mine: "My publications",
  queue: "Curator queue",
};

function failureDetail(failure: unknown): string {
  if (typeof failure !== "object" || failure === null) return "The library request failed. Please try again.";
  const value = failure as { error_type?: unknown; detail?: unknown; current_state?: unknown; sources?: unknown };
  const detail = typeof value.detail === "string" ? value.detail : "The library request failed. Please try again.";
  if (value.error_type === "library_entry_needs_profile_bound_source" && Array.isArray(value.sources)) {
    return `${detail} Affected sources: ${value.sources.filter((item): item is string => typeof item === "string").join(", ")}.`;
  }
  return typeof value.current_state === "string" ? `${detail} Current state: ${value.current_state}.` : detail;
}

function errorType(failure: unknown): string | null {
  if (typeof failure !== "object" || failure === null || !("error_type" in failure)) return null;
  return typeof failure.error_type === "string" ? failure.error_type : null;
}

interface PendingAction {
  entry: LibraryEntry;
  action: LibraryCuratorAction;
}

/** A public projection is forked by the server into the user's own session. */
export function LibraryDialog({ onClose }: { onClose: () => void }): JSX.Element {
  const modalRef = useRef<HTMLDivElement>(null);
  const currentIdentityId = useAuthStore((state) => state.user?.user_id ?? "");
  const roles = useMailboxStore((state) => state.summary?.roles ?? []);
  const refreshSummary = useMailboxStore((state) => state.refreshSummary);
  const activeSessionId = useSessionStore((state) => state.activeSessionId);
  const hasState = useSessionStore((state) => state.compositionState !== null);
  const [view, setView] = useState<LibraryView>("accepted");
  const [entries, setEntries] = useState<LibraryEntry[] | null>(null);
  const [reload, setReload] = useState(0);
  const [governanceOff, setGovernanceOff] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState("");
  const [pending, setPending] = useState<PendingAction | null>(null);
  const [note, setNote] = useState("");
  const curator = roles.includes("curator");
  const publisher = roles.includes("user");
  useFocusTrap(modalRef, true);

  useEffect(() => { void refreshSummary(); }, [refreshSummary]);

  useEffect(() => {
    if (view === "queue" && !curator) setView("accepted");
  }, [curator, view]);

  useEffect(() => {
    let live = true;
    setEntries(null);
    void library.fetchLibrary(view).then((result) => {
      if (live) setEntries(result.entries);
    }).catch((failure: unknown) => {
      if (!live) return;
      if (errorType(failure) === "workflow_governance_off") setGovernanceOff(true);
      else setError(failureDetail(failure));
    });
    return () => { live = false; };
  }, [view, reload]);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) { if (event.key === "Escape") onClose(); }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [onClose]);

  function chooseView(next: LibraryView): void {
    if (busy || (next === "queue" && !curator)) return;
    setError(null);
    setNotice(null);
    setPending(null);
    setView(next);
  }

  function onTabsKeyDown(event: React.KeyboardEvent<HTMLDivElement>): void {
    const available = TABS.filter((tab) => tab !== "queue" || curator);
    const index = available.indexOf(view);
    const next = event.key === "ArrowRight" ? available[(index + 1) % available.length]
      : event.key === "ArrowLeft" ? available[(index - 1 + available.length) % available.length]
        : event.key === "Home" ? available[0] : event.key === "End" ? available[available.length - 1] : null;
    if (next === null) return;
    event.preventDefault();
    chooseView(next);
    event.currentTarget.querySelector<HTMLButtonElement>(`[data-library-view="${next}"]`)?.focus();
  }

  async function fork(entry: LibraryEntry): Promise<void> {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      const created = await library.forkLibraryEntry(entry.entry_id);
      const sessions = useSessionStore.getState();
      await sessions.loadSessions();
      await sessions.selectSession(created.session_id);
      onClose();
    } catch (failure) {
      setError(failureDetail(failure));
      if (errorType(failure) === "library_entry_not_forkable") {
        setEntries((current) => current?.filter((item) => item.entry_id !== entry.entry_id) ?? null);
      }
    } finally {
      setBusy(false);
    }
  }

  async function publish(): Promise<void> {
    if (activeSessionId === null || !hasState || !title.trim()) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await library.publishLibraryEntry(activeSessionId, title);
      setTitle("");
      setNotice("Published for curator review.");
      setView("mine");
      setReload((value) => value + 1);
    } catch (failure) {
      setError(failureDetail(failure));
    } finally {
      setBusy(false);
    }
  }

  async function curate(): Promise<void> {
    if (pending === null || (pending.action === "reject" && !note.trim())) return;
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      await library.curateLibraryEntry(pending.entry.entry_id, pending.action, note);
      setNotice(`${pending.entry.title} was ${pending.action === "accept" ? "accepted" : pending.action === "reject" ? "rejected" : pending.action === "deprecate" ? "deprecated" : "recalled"}.`);
      setPending(null);
      setNote("");
      setReload((value) => value + 1);
    } catch (failure) {
      setError(failureDetail(failure));
      if (errorType(failure) === "library_entry_already_curated" || errorType(failure) === "library_entry_not_forkable") {
        setPending(null);
        setReload((value) => value + 1);
      }
    } finally {
      setBusy(false);
    }
  }

  function startAction(entry: LibraryEntry, action: LibraryCuratorAction): void {
    setError(null);
    setNotice(null);
    setPending({ entry, action });
    setNote("");
  }

  return <>
    <div role="presentation" className="app-dialog-backdrop" onClick={onClose} />
    <div ref={modalRef} role="dialog" aria-modal="true" aria-labelledby="library-title" className="app-dialog settings-dialog settings-dialog-wide library-dialog">
      <div className="secrets-panel-header">
        <h2 id="library-title" className="secrets-panel-title">Shared library</h2>
        <Button variant="bare" className="dialog-close" aria-label="Close shared library" onClick={onClose}>×</Button>
      </div>
      <div className="secrets-panel-body library-body">
        {governanceOff ? <p>The shared library is not enabled on this deployment.</p> : <>
          {publisher && <form className="library-publish" onSubmit={(event) => { event.preventDefault(); void publish(); }}>
            <h3>Publish current pipeline</h3>
            <Input label="Title" value={title} maxLength={200} onChange={(event) => setTitle(event.target.value)} disabled={busy || activeSessionId === null || !hasState} />
            <Button type="submit" disabled={busy || activeSessionId === null || !hasState || !title.trim()}>Publish for review</Button>
            {(activeSessionId === null || !hasState) && <p>Open a session with a pipeline to publish it.</p>}
          </form>}
          <div role="tablist" aria-label="Library sections" className="library-tabs" onKeyDown={onTabsKeyDown}>
            {TABS.filter((tab) => tab !== "queue" || curator).map((tab) => <Button
              key={tab} role="tab" data-library-view={tab} aria-selected={view === tab} aria-controls={`library-${tab}`}
              tabIndex={view === tab ? 0 : -1} disabled={busy} onClick={() => chooseView(tab)}>{TAB_LABELS[tab]}</Button>)}
          </div>
          <div id={`library-${view}`} role="tabpanel" aria-label={TAB_LABELS[view]}>
            {entries === null ? error === null && <p>Loading the library</p> : entries.length === 0
              ? <p>{view === "accepted" ? "No entries have been accepted yet." : view === "queue" ? "No publications await curation." : "You have not published any entries."}</p>
              : <ul className="library-list">{entries.map((entry) => <li key={entry.entry_id} className="library-entry">
                <strong>{entry.title}</strong>
                <span>Version {entry.version} · Origin compartment: {entry.compartment_id}</span>
                <span>Publisher: {entry.published_by_identity_id}</span>
                <span className="library-digest">Content digest: {entry.payload_digest}</span>
                {view === "mine" && <span>Status: {entry.state}</span>}
                {entry.state === "deprecated" && <span className="library-deprecated">Deprecated</span>}
                {entry.rejection_note !== null && view === "mine" && <span>Rejection note: {entry.rejection_note}</span>}
                <div className="library-actions">
                  {view === "accepted" && <Button compact disabled={busy} aria-label={`Fork ${entry.title}`} onClick={() => void fork(entry)}>Fork</Button>}
                  {curator && currentIdentityId !== entry.published_by_identity_id && view === "queue" && <>
                    <Button compact disabled={busy} onClick={() => startAction(entry, "accept")}>Accept {entry.title}</Button>
                    <Button compact disabled={busy} onClick={() => startAction(entry, "reject")}>Reject {entry.title}</Button>
                  </>}
                  {curator && currentIdentityId !== entry.published_by_identity_id && view === "accepted" && <>
                    {entry.state === "accepted" && <Button compact disabled={busy} onClick={() => startAction(entry, "deprecate")}>Deprecate {entry.title}</Button>}
                    <Button compact disabled={busy} onClick={() => startAction(entry, "recall")}>Recall {entry.title}</Button>
                  </>}
                </div>
              </li>)}</ul>}
          </div>
          {pending !== null && <div className="library-confirm">
            <h3>{pending.action[0].toUpperCase() + pending.action.slice(1)} {pending.entry.title}?</h3>
            <label className="library-field">{pending.action === "reject" ? "Reason (required)" : "Note (optional)"}
              <textarea value={note} maxLength={4096} onChange={(event) => setNote(event.target.value)} disabled={busy} />
            </label>
            {new TextEncoder().encode(note).length > 4096 && <p role="status">The note exceeds the 4 KiB limit.</p>}
            <div className="library-actions">
              <Button disabled={busy || (pending.action === "reject" && !note.trim()) || new TextEncoder().encode(note).length > 4096} onClick={() => void curate()}>Confirm {pending.action}</Button>
              <Button variant="bare" disabled={busy} onClick={() => setPending(null)}>Cancel</Button>
            </div>
          </div>}
        </>}
        {notice !== null && <p role="status">{notice}</p>}
        {error !== null && <p role="alert" className="library-error">{error}</p>}
      </div>
    </div>
  </>;
}
