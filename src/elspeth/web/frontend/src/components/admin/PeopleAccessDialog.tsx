import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { adminErrorMessage } from "@/api/identityAdmin";
import { PEOPLE_PAGE_SIZE, fetchPeopleCapabilities, isAbort, listPeople } from "@/api/people";
import { Button } from "@/components/ui";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import type { PeopleCapabilities, PeopleQuery, PersonRecord } from "@/types/people";
import { AddPersonForm } from "./AddPersonForm";
import { GeneratedPassword, type GeneratedCredential } from "./GeneratedPassword";
import { PeopleDirectory, type DirectoryLoad } from "./PeopleDirectory";
import { PersonDetail } from "./PersonDetail";
import { PeoplePanelContext, type PeoplePanelServices } from "./peoplePanel";
import "./admin.css";

const FIRST_QUERY: PeopleQuery = { q: "", status: "all", provider: "all", type: "people", offset: 0 };

type Leave = { reason: "unsaved" | "password"; proceed: () => void };

interface Props {
  onClose: () => void;
  /** Called when the caller turns out to hold neither capability any more. */
  onUnavailable: () => void;
}

/**
 * People & access: one place to find a person and manage their access, roles,
 * approvers, limits and sign-in.
 *
 * The panel is ONE modal and one focus trap. Confirmations are inline
 * subviews inside it, so there is never a second dialog competing for focus,
 * and Escape unwinds them innermost-first before it closes the panel.
 *
 * Nothing here is an authorization decision. Capabilities are re-read on open
 * and whenever a route refuses; the half of the panel a caller has lost is
 * removed at once and the half they still hold stays usable. On sign-out the
 * App unmounts the panel, which discards every piece of state below,
 * including any password on screen.
 */
export function PeopleAccessDialog({ onClose, onUnavailable }: Props): JSX.Element {
  const modalRef = useRef<HTMLDivElement>(null);
  // Capabilities have not loaded when the trap chooses its first target, so
  // "first focusable" would be the close button. Start on the title instead:
  // it names the dialog and is there from the first paint.
  useFocusTrap(modalRef, true, "#people-access-title");

  const [capabilities, setCapabilities] = useState<PeopleCapabilities | null>(null);
  const [capabilityError, setCapabilityError] = useState<string | null>(null);
  const [capabilityAttempt, setCapabilityAttempt] = useState(0);
  const [query, setQuery] = useState<PeopleQuery>(FIRST_QUERY);
  const [draftText, setDraftText] = useState("");
  const [load, setLoad] = useState<DirectoryLoad>({ status: "loading" });
  const [activeAdminCount, setActiveAdminCount] = useState<number | null>(null);
  const [refreshTick, setRefreshTick] = useState(0);
  const [selected, setSelected] = useState<{ key: string; seed: PersonRecord | null } | null>(null);
  const [adding, setAdding] = useState(false);
  const [view, setView] = useState<"list" | "detail">("list");
  // Every undismissed one-time password stays on screen. A second account's
  // password must not replace the first: neither can be shown again. A new
  // password for the SAME account does replace the old one, which no longer works.
  const [credentials, setCredentials] = useState<GeneratedCredential[]>([]);
  const addCredential = useCallback((next: GeneratedCredential) => {
    setCredentials((current) => [...current.filter((held) => held.username !== next.username), next]);
  }, []);
  const [leave, setLeave] = useState<Leave | null>(null);
  const [status, setStatus] = useState<string | null>(null);

  const escapeHandlers = useRef(new Map<string, () => void>());
  const dirtyOwners = useRef(new Set<string>());
  const rows = useRef(new Map<string, HTMLButtonElement>());
  const leaveRef = useRef<HTMLDivElement>(null);
  const returnFocusTo = useRef<string | null>(null);
  const addButtonRef = useRef<HTMLButtonElement>(null);

  // ── Capabilities ────────────────────────────────────────────────────────
  useEffect(() => {
    const controller = new AbortController();
    void fetchPeopleCapabilities(controller.signal).then(
      (result) => {
        setCapabilityError(null);
        if (!result.identity_admin && !result.local_accounts) { onUnavailable(); return; }
        setCapabilities((current) => {
          if (current !== null && (current.identity_admin !== result.identity_admin || current.local_accounts !== result.local_accounts)) {
            // A capability changed under an open panel: what was selected or
            // filtered may no longer be theirs to see. Start from a clean roster.
            setSelected(null);
            setAdding(false);
            setView("list");
            setQuery(FIRST_QUERY);
            setDraftText("");
            setStatus("Your permissions changed. The panel now shows what you can manage.");
          }
          return result;
        });
      },
      (error: unknown) => { if (!isAbort(error)) setCapabilityError(adminErrorMessage(error, "Could not check what you can manage")); },
    );
    return () => controller.abort();
  }, [capabilityAttempt, onUnavailable]);

  // ── Directory ───────────────────────────────────────────────────────────
  useEffect(() => {
    if (capabilities === null) return;
    const controller = new AbortController();
    setLoad({ status: "loading" });
    void listPeople(query, controller.signal).then(
      (page) => {
        setLoad({ status: "ready", people: page.people, hasMore: page.has_more });
        setActiveAdminCount(page.active_human_admin_count);
      },
      (error: unknown) => {
        if (isAbort(error)) return;
        setLoad({ status: "error", message: adminErrorMessage(error, "Could not load people") });
        if (typeof error === "object" && error !== null && "status" in error && (error.status === 404 || error.status === 422)) setCapabilityAttempt((value) => value + 1);
      },
    );
    return () => controller.abort();
  }, [capabilities, query, refreshTick]);

  // ── Services for the sections ───────────────────────────────────────────
  const services = useMemo<PeoplePanelServices>(() => ({
    setEscapeHandler: (owner, handler) => {
      escapeHandlers.current.delete(owner);
      if (handler !== null) escapeHandlers.current.set(owner, handler);
    },
    setDirty: (owner, dirty) => {
      if (dirty) dirtyOwners.current.add(owner); else dirtyOwners.current.delete(owner);
    },
    onAuthorityRefused: () => setCapabilityAttempt((value) => value + 1),
  }), []);

  /** Run `proceed` now, or ask first when it would throw away typed input. */
  const guard = useCallback((proceed: () => void) => {
    if (dirtyOwners.current.size > 0) setLeave({ reason: "unsaved", proceed });
    else proceed();
  }, []);

  const requestClose = useCallback(() => {
    if (dirtyOwners.current.size > 0) setLeave({ reason: "unsaved", proceed: onClose });
    else if (credentials.length > 0) setLeave({ reason: "password", proceed: onClose });
    else onClose();
  }, [credentials, onClose]);

  useEffect(() => { if (leave !== null) leaveRef.current?.focus(); }, [leave]);

  useEffect(() => {
    function onKeyDown(event: KeyboardEvent) {
      if (event.key !== "Escape") return;
      if (leave !== null) { setLeave(null); return; }
      const handlers = [...escapeHandlers.current.values()];
      const innermost = handlers[handlers.length - 1];
      if (innermost !== undefined) { innermost(); return; }
      requestClose();
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [leave, requestClose]);

  // ── Selection ───────────────────────────────────────────────────────────
  const select = useCallback((person: PersonRecord) => {
    guard(() => { setAdding(false); setStatus(null); setSelected({ key: person.key, seed: person }); setView("detail"); });
  }, [guard]);

  const backToList = useCallback(() => {
    guard(() => { returnFocusTo.current = selected?.key ?? null; setView("list"); setAdding(false); });
  }, [guard, selected]);

  useEffect(() => {
    if (view !== "list" || returnFocusTo.current === null) return;
    // The row may have left the list (approved out of "Pending access"); fall
    // back to the list's first control rather than leaving focus on nothing.
    const row = rows.current.get(returnFocusTo.current);
    returnFocusTo.current = null;
    (row ?? modalRef.current?.querySelector<HTMLElement>("input[type=search]"))?.focus();
  }, [view, load]);

  const onPersonChanged = useCallback((key: string) => {
    setSelected((current) => (current === null ? current : current.key === key ? current : { key, seed: null }));
    setRefreshTick((value) => value + 1);
  }, []);

  const onGone = useCallback((message: string) => {
    setSelected(null);
    setView("list");
    setStatus(message);
    setRefreshTick((value) => value + 1);
  }, []);

  const rowRef = useCallback((key: string, element: HTMLButtonElement | null) => {
    if (element === null) rows.current.delete(key); else rows.current.set(key, element);
  }, []);

  const canAdd = capabilities !== null && (capabilities.local_accounts || capabilities.identity_admin);

  return (
    <PeoplePanelContext.Provider value={services}>
      <div role="presentation" className="app-dialog-backdrop" onClick={requestClose} />
      <div ref={modalRef} role="dialog" aria-modal="true" aria-labelledby="people-access-title" aria-describedby="people-access-intro" className="app-dialog settings-dialog people-dialog">
        <div className="secrets-panel-header">
          <h2 id="people-access-title" tabIndex={-1} className="secrets-panel-title">People &amp; access</h2>
          <div className="people-header-actions">
            {canAdd && <Button ref={addButtonRef} compact disabled={adding} onClick={() => guard(() => { setStatus(null); setAdding(true); setView("detail"); })}>Add person</Button>}
            <Button variant="bare" aria-label="Close People & access" className="dialog-close" onClick={requestClose}>×</Button>
          </div>
        </div>
        <div className="secrets-panel-body people-body">
          <p id="people-access-intro" className="user-admin-intro">Manage access, permissions and sign-in accounts. Your pipeline and draft are untouched underneath.</p>

          {leave !== null && (
            <div ref={leaveRef} tabIndex={-1} role="alertdialog" aria-labelledby="people-leave-title" className="people-notice people-notice-uncertain">
              <span id="people-leave-title">{leave.reason === "password"
                ? `The password for ${credentials.map((held) => held.username).join(" and ") || "this account"} is still on screen and cannot be shown again. Close anyway?`
                : "You have unsaved changes. Discard them?"}</span>
              <Button compact variant="danger" onClick={() => { const { proceed } = leave; dirtyOwners.current.clear(); setLeave(null); proceed(); }}>{leave.reason === "password" ? "Close without the password" : "Discard changes"}</Button>
              <Button compact onClick={() => setLeave(null)}>{leave.reason === "password" ? "Keep open" : "Keep editing"}</Button>
            </div>
          )}

          {credentials.map((held) => <GeneratedPassword key={held.username} credential={held} onDismiss={() => setCredentials((current) => current.filter((other) => other !== held))} />)}
          {status !== null && <p role="status" className="people-notice people-notice-saved">{status}</p>}
          {activeAdminCount === 1 && <p role="status" className="identity-admin-advisory">This deployment has one active human administrator. Add another before changing that administrator's access.</p>}

          {capabilities === null ? (
            capabilityError !== null
              ? <div role="alert" className="people-notice people-notice-rejected"><span>{capabilityError}</span><Button compact onClick={() => setCapabilityAttempt((value) => value + 1)}>Retry</Button></div>
              : <p role="status">Checking what you can manage…</p>
          ) : (
            <div className="people-layout" data-view={view}>
              <div className="people-pane people-pane-list">
                <PeopleDirectory capabilities={capabilities} query={query} draftText={draftText} load={load} selectedKey={adding ? null : selected?.key ?? null} pageSize={PEOPLE_PAGE_SIZE}
                  onDraftText={setDraftText} onQuery={(patch) => guard(() => setQuery((current) => ({ ...current, ...patch })))} onSelect={select} onRetry={() => setRefreshTick((value) => value + 1)} rowRef={rowRef} />
              </div>
              <div className="people-pane people-pane-detail">
                <Button compact className="people-back" onClick={backToList}>{query.status === "pending" ? "Back to pending access" : "Back to people"}</Button>
                {adding ? (
                  <AddPersonForm capabilities={capabilities} onCredential={addCredential} onCancel={() => { setAdding(false); setView("list"); setTimeout(() => addButtonRef.current?.focus(), 0); }}
                    onAdded={(key) => { dirtyOwners.current.clear(); setAdding(false); setSelected({ key, seed: null }); setView("detail"); setRefreshTick((value) => value + 1); }} />
                ) : selected !== null ? (
                  <PersonDetail key={selected.key} personKey={selected.key} initial={selected.seed} activeAdminCount={activeAdminCount} onCredential={addCredential} onPersonChanged={onPersonChanged} onGone={onGone} />
                ) : (
                  <p className="people-empty-detail">Select a person to manage their access, roles, approvers, limits and sign-in.</p>
                )}
              </div>
            </div>
          )}
        </div>
      </div>
    </PeoplePanelContext.Provider>
  );
}
