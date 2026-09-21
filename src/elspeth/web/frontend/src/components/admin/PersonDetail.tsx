import { type JSX, useCallback, useEffect, useId, useRef, useState } from "react";
import * as admin from "@/api/identityAdmin";
import { errorStatus, fetchPerson, isAbort, isUncertainOutcome, personDisambiguator, personName } from "@/api/people";
import { Button, Input } from "@/components/ui";
import type { ActivationRole } from "@/types/identityAdmin";
import type { LocalAccountPerson, PersonRecord } from "@/types/people";
import { AccessSection } from "./AccessSection";
import type { GeneratedCredential } from "./GeneratedPassword";
import { QuotaEditor } from "./QuotaEditor";
import { RelationshipsEditor } from "./RelationshipsEditor";
import { RolesEditor } from "./RolesEditor";
import { SignInSection } from "./SignInSection";
import { ROLE_LABEL, formatInstant } from "./peopleFormat";
import { useCancelToTrigger, useCopiedReset, usePeoplePanel, useSubview } from "./peoplePanel";

type Tab = "roles" | "approvers" | "usage";
const TAB_LABEL: Record<Tab, string> = { roles: "Roles", approvers: "Approvers", usage: "Usage & limits" };
const ACTIVATION_ROLES: ActivationRole[] = ["user", "approver", "reviewer", "none"];

interface Props {
  personKey: string;
  /** The record the directory already holds, shown at once while the direct read runs. */
  initial: PersonRecord | null;
  quotasEnabled: boolean;
  onCredential: (credential: GeneratedCredential) => void;
  /** This person's record changed, or (with `to`) access was set up and their key moved: refresh the directory too. */
  onPersonChanged: (from: string, to?: string) => void;
  onGone: (message: string) => void;
}

type Load = { status: "loading" } | { status: "ready" } | { status: "error"; message: string; unavailableSource: boolean };

function SetUpAccess({ person, onDone }: { person: LocalAccountPerson; onDone: (identityKey: string) => void }): JSX.Element {
  const { onAuthorityRefused } = usePeoplePanel();
  const name = personName(person);
  const [open, setOpen] = useState(false);
  const [role, setRole] = useState<ActivationRole>("user");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [failure, setFailure] = useState<string | null>(null);
  const close = useCallback(() => { setOpen(false); setFailure(null); }, []);
  const { triggerRef, cancel } = useCancelToTrigger<HTMLButtonElement>(close);
  useSubview(`setup:${person.key}`, open, note.trim() !== "", cancel);

  if (!open) return <div><Button ref={triggerRef} variant="primary" onClick={() => setOpen(true)}>Set up access</Button></div>;
  return (
    <form className="identity-admin-form" onSubmit={(event) => {
      event.preventDefault();
      setBusy(true);
      setFailure(null);
      const username = person.local_account.username;
      // Exact correlation: the identity is the LOCAL provider's, and its subject IS the username.
      void admin.preProvisionIdentity({ provider: "local", subject: username, username, role, note: note.trim() }).then(
        (result) => onDone(`identity:${result.identity.identity_id}`),
        (error: unknown) => {
          const status = errorStatus(error);
          if (status === 404 || status === 403) onAuthorityRefused();
          setFailure(isUncertainOutcome(error)
            ? "The connection dropped before the server answered, so access may or may not have been set up. Reload this person before trying again."
            : admin.adminErrorMessage(error, "Access was not set up"));
          setBusy(false);
        },
      );
    }}>
      <h4>Set up access for {name}</h4>
      <p>The local account <code>{person.local_account.username}</code> already exists and is kept whatever happens here. This step gives it access.</p>
      {failure !== null && <p role="alert" className="people-notice people-notice-rejected">{failure}</p>}
      <div className="identity-admin-fields">
        <label className="identity-admin-field">Initial role
          <select className="input" value={role} onChange={(event) => setRole(event.target.value as ActivationRole)}>
            {ACTIVATION_ROLES.map((value) => <option key={value} value={value}>{value === "none" ? "No role yet" : ROLE_LABEL[value]}</option>)}
          </select>
        </label>
        <Input label="Note (required)" value={note} required maxLength={512} onChange={(event) => setNote(event.target.value)} />
      </div>
      <div className="identity-admin-actions">
        <Button type="submit" variant="primary" disabled={busy || note.trim() === ""}>{failure === null ? "Set up access" : "Retry access setup"}</Button>
        <Button disabled={busy} onClick={cancel}>Cancel</Button>
      </div>
    </form>
  );
}

function CopyId({ value }: { value: string }): JSX.Element {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  useCopiedReset(state === "copied", () => setState("idle"));
  return (
    <>
      <code className="people-wrap">{value}</code>{" "}
      <Button compact aria-label="Copy identity ID" onClick={() => void navigator.clipboard.writeText(value).then(() => setState("copied"), () => setState("failed"))}>{state === "copied" ? "Copied" : "Copy"}</Button>
      {state === "failed" && <span role="alert"> Copy failed. Select the ID and copy it manually.</span>}
    </>
  );
}

/** Everything about one person. Bound to a stable key, never to a row position. */
export function PersonDetail({ personKey, initial, quotasEnabled, onCredential, onPersonChanged, onGone }: Props): JSX.Element {
  const { guardLeave } = usePeoplePanel();
  const [person, setPerson] = useState<PersonRecord | null>(initial?.key === personKey ? initial : null);
  const [load, setLoad] = useState<Load>({ status: "loading" });
  const [tab, setTab] = useState<Tab>("roles");
  const headingRef = useRef<HTMLHeadingElement>(null);
  const tabsId = useId();
  // Every read is tagged with the key it was issued for. A slow answer for
  // the previous person must never be painted under this person's heading.
  const currentKey = useRef(personKey);
  currentKey.current = personKey;
  // `initial` seeds the first paint only. Read through a ref so a directory
  // refresh, which hands down a new object for the same person, does not
  // restart this person's load.
  const initialRef = useRef(initial);
  initialRef.current = initial;

  const read = useCallback(async (signal?: AbortSignal): Promise<void> => {
    const requested = personKey;
    const response = await fetchPerson(requested, signal);
    if (currentKey.current !== requested) return;
    if (response.person.key !== requested) { onPersonChanged(requested, response.person.key); return; }
    setPerson(response.person);
    setLoad({ status: "ready" });
  }, [personKey, onPersonChanged]);

  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    const seed = initialRef.current;
    setPerson(seed?.key === personKey ? seed : null);
    setLoad({ status: "loading" });
    setTab("roles");
    read(controller.signal).catch((error: unknown) => {
      if (isAbort(error) || currentKey.current !== personKey) return;
      if (errorStatus(error) === 404) { onGone("That person is no longer available to you."); return; }
      setLoad({ status: "error", message: admin.adminErrorMessage(error, "Could not load this person"), unavailableSource: errorStatus(error) === 503 });
    });
    return () => controller.abort();
  }, [personKey, read, attempt, onGone]);

  useEffect(() => { headingRef.current?.focus(); }, [personKey]);

  // A role write can change what the person record says (granting a second
  // administrator ends "the only active administrator"), so it re-reads this
  // person as well as telling the directory. Best effort and silent: the role
  // editor has already reported its own write, the flag is advisory, and the
  // next open reads it again.
  const notifyChanged = useCallback(() => {
    onPersonChanged(personKey);
    read().catch(() => undefined);
  }, [onPersonChanged, personKey, read]);

  const reloadPerson = useCallback(async () => {
    await read();
    onPersonChanged(personKey);
  }, [read, onPersonChanged, personKey]);

  if (person === null) {
    return (
      <div className="people-detail">
        {load.status === "error"
          ? <div role="alert" className="people-notice people-notice-rejected"><span>{load.message}</span><Button compact onClick={() => setAttempt((value) => value + 1)}>Retry</Button></div>
          : <p>Loading person…</p>}
      </div>
    );
  }

  const name = personName(person);
  const isService = person.record_type === "identity" && person.identity.kind === "service";
  const tabs: Tab[] = person.record_type === "identity" ? (isService ? ["roles", "usage"] : ["roles", "approvers", "usage"]) : [];

  return (
    <div className="people-detail">
      <div className="people-detail-header">
        <h3 ref={headingRef} tabIndex={-1} className="people-detail-name">{name}</h3>
        <p className="people-grant-meta people-wrap">{personDisambiguator(person)}{isService && " · Service account (operator-managed)"}</p>
      </div>
      {load.status === "error" && (
        <div role="alert" className="people-notice people-notice-rejected">
          <span>{load.unavailableSource ? "Access details unavailable. " : ""}{load.message} Showing the last details loaded.</span>
          <Button compact onClick={() => setAttempt((value) => value + 1)}>Retry</Button>
        </div>
      )}

      {person.record_type === "local_account" ? (
        <>
          <h4>Access</h4>
          {person.access === "not_set_up" ? (
            <section aria-label={`Access for ${name}`} className="people-section">
              <div className="people-access-status"><span className="people-state people-state-not_set_up">Access not set up</span><span className="people-grant-meta">{name} has a local account and no access yet. They cannot use ELSPETH until access is set up.</span></div>
              {person.actions.set_up_access
                ? <SetUpAccess person={person} onDone={(identityKey) => onPersonChanged(person.key, identityKey)} />
                : <p>An access administrator must set up access.</p>}
            </section>
          ) : (
            <section aria-label={`Access for ${name}`} className="people-section"><p>Access is managed by an access administrator. You can manage this person's local sign-in account below.</p></section>
          )}
          <h4>Sign-in</h4>
          <SignInSection personName={name} provider="local" account={person.local_account} identityRetired={null} canManage={person.actions.manage_credentials} isSelf={person.actions.is_self} onCredential={onCredential} onChanged={reloadPerson} onGone={() => onGone(`The local account for ${name} is deleted.`)} />
        </>
      ) : (
        <>
          <h4>Access</h4>
          <AccessSection person={person} personName={name} onChanged={reloadPerson} />

          {/* The two fixed sections sit together; the tabs end the page. With
              Sign-in below the tabs it moved every time the tab height changed. */}
          <h4>Sign-in</h4>
          <SignInSection personName={name} provider={person.identity.provider} account={person.local_account} identityRetired={person.retired} canManage={person.actions.manage_credentials} isSelf={person.actions.is_self} onCredential={onCredential} onChanged={reloadPerson} onGone={() => onGone("That person is no longer available to you.")} />

          {!person.retired && (
            <>
              <div role="tablist" aria-label={`Sections for ${name}`} className="identity-admin-tabs" onKeyDown={(event) => {
                const index = tabs.indexOf(tab);
                const next = event.key === "ArrowRight" ? (index + 1) % tabs.length : event.key === "ArrowLeft" ? (index - 1 + tabs.length) % tabs.length : event.key === "Home" ? 0 : event.key === "End" ? tabs.length - 1 : -1;
                if (next < 0) return;
                event.preventDefault();
                // Leaving a section unmounts its form, so it asks first like leaving the person does.
                const buttons = event.currentTarget.querySelectorAll<HTMLButtonElement>("[role=tab]");
                guardLeave(() => { setTab(tabs[next]); buttons[next].focus(); });
              }}>
                {tabs.map((value) => <Button key={value} id={`${tabsId}-tab-${value}`} role="tab" tabIndex={tab === value ? 0 : -1} aria-selected={tab === value} aria-controls={`${tabsId}-panel`} variant="bare" className="identity-admin-tab" onClick={() => { if (value !== tab) guardLeave(() => setTab(value)); }}>{TAB_LABEL[value]}</Button>)}
              </div>
              <div id={`${tabsId}-panel`} role="tabpanel" aria-labelledby={`${tabsId}-tab-${tab}`}>
                {tab === "roles" && <RolesEditor key={`${person.key}:${person.identity.access_state}`} identityId={person.identity.identity_id} personName={name} kind={person.identity.kind} onChanged={notifyChanged} />}
                {tab === "approvers" && <RelationshipsEditor key={person.key} identityId={person.identity.identity_id} personName={name} />}
                {tab === "usage" && (person.identity.access_state === "active"
                  ? <QuotaEditor key={person.key} identityId={person.identity.identity_id} personName={name} capsEnabled={quotasEnabled} />
                  : <p>Usage and limits are shown once {name}'s access is active.</p>)}
              </div>
            </>
          )}

          <details className="people-advanced">
            <summary>Advanced details</summary>
            <dl className="people-facts">
              <dt>Identity ID</dt><dd><CopyId value={person.identity.identity_id} /></dd>
              <dt>Provider subject</dt><dd><code className="people-wrap">{person.identity.subject}</code></dd>
              <dt>Organisation</dt><dd>{person.identity.organisation_id ?? "None recorded"}</dd>
              <dt>First seen</dt><dd>{formatInstant(person.identity.first_seen_at)}</dd>
              <dt>Last sign-in</dt><dd>{person.identity.last_login_at === null ? "Never, or not shown for a person not yet approved" : formatInstant(person.identity.last_login_at)}</dd>
              {person.identity.pre_provisioned_at !== null && <><dt>Prepared in advance</dt><dd>{formatInstant(person.identity.pre_provisioned_at)}</dd></>}
              {person.identity.activated_at !== null && <><dt>First approved</dt><dd>{formatInstant(person.identity.activated_at)}</dd></>}
            </dl>
          </details>
        </>
      )}
    </div>
  );
}
