import { useCallback, useState } from "react";
import * as api from "@/api/client";
import * as admin from "@/api/identityAdmin";
import { errorStatus, isUncertainOutcome, localKey } from "@/api/people";
import { Button, Input } from "@/components/ui";
import type { ActivationRole, HumanProvisionProvider } from "@/types/identityAdmin";
import type { PeopleCapabilities } from "@/types/people";
import type { GeneratedCredential } from "./GeneratedPassword";
import { ROLE_LABEL } from "./peopleFormat";
import { usePeoplePanel, useSubview } from "./peoplePanel";

const SSO_PROVIDERS: Exclude<HumanProvisionProvider, "local">[] = ["oidc", "entra", "vanguard", "google"];
const ACTIVATION_ROLES: ActivationRole[] = ["user", "approver", "reviewer", "none"];

type Mode = "local" | "external";

interface Props {
  capabilities: PeopleCapabilities;
  onCredential: (credential: GeneratedCredential) => void;
  /** Select the new person and refresh the directory. */
  onAdded: (key: string) => void;
  onCancel: () => void;
}

/**
 * Two honest ways to add a person, each offered only to a caller who can do it.
 *
 * Creating a local account makes a PASSWORD. It does not grant access, and the
 * form says so: the account appears as "Access not set up" and access is a
 * second, separate step in a separate store. If that second step fails the
 * account is kept and the step is retried on its own; the creation is never
 * re-run as a generic retry and never rolled back.
 */
export function AddPersonForm({ capabilities, onCredential, onAdded, onCancel }: Props): JSX.Element {
  const { onAuthorityRefused } = usePeoplePanel();
  const modes: Mode[] = [...(capabilities.local_accounts ? ["local" as const] : []), ...(capabilities.identity_admin ? ["external" as const] : [])];
  const [mode, setMode] = useState<Mode>(modes[0]);
  const [username, setUsername] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [email, setEmail] = useState("");
  const configured = SSO_PROVIDERS.find((value) => value === capabilities.auth_provider);
  const [provider, setProvider] = useState<(typeof SSO_PROVIDERS)[number]>(configured ?? "oidc");
  const [subject, setSubject] = useState("");
  const [organisation, setOrganisation] = useState("");
  const [role, setRole] = useState<ActivationRole>("user");
  const [note, setNote] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<{ message: string; uncertain: boolean } | null>(null);

  const dirty = [username, displayName, email, subject, organisation, note].some((value) => value.trim() !== "");
  const cancel = useCallback(() => onCancel(), [onCancel]);
  useSubview("add-person", true, dirty, cancel);

  function fail(failure: unknown, fallback: string): void {
    const status = errorStatus(failure);
    if (status === 404 || status === 403) onAuthorityRefused();
    setError(isUncertainOutcome(failure)
      ? { uncertain: true, message: "The connection dropped before the server answered, so this may or may not have been created. Search for the person before trying again." }
      : { uncertain: false, message: admin.adminErrorMessage(failure, fallback) });
  }

  async function createLocal(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const trimmedEmail = email.trim();
      const result = await api.createAdminUser({ username: username.trim(), display_name: displayName.trim(), ...(trimmedEmail !== "" ? { email: trimmedEmail } : {}) });
      onCredential({ username: result.user_id, password: result.password, cause: "created" });
      onAdded(localKey(result.user_id));
    } catch (failure) {
      fail(failure, "Account was not created");
    } finally {
      setBusy(false);
    }
  }

  async function prepareExternal(): Promise<void> {
    setBusy(true);
    setError(null);
    try {
      const trimmedOrganisation = organisation.trim();
      const result = await admin.preProvisionIdentity({ provider, subject: subject.trim(), ...(trimmedOrganisation !== "" ? { organisation_id: trimmedOrganisation } : {}), role, note: note.trim() });
      onAdded(`identity:${result.identity.identity_id}`);
    } catch (failure) {
      fail(failure, "Access was not prepared");
    } finally {
      setBusy(false);
    }
  }

  return (
    <section aria-labelledby="people-add-heading" className="people-section people-add">
      <h3 id="people-add-heading">Add person</h3>
      {modes.length > 1 && (
        <fieldset className="people-direction" disabled={busy}>
          <legend>How will this person sign in?</legend>
          <label><Input type="radio" name="people-add-mode" checked={mode === "local"} onChange={() => { setMode("local"); setError(null); }} /> Create a local account</label>
          <label><Input type="radio" name="people-add-mode" checked={mode === "external"} onChange={() => { setMode("external"); setError(null); }} /> Prepare access for someone who signs in through a provider</label>
        </fieldset>
      )}
      {error !== null && <div role="alert" className={`people-notice people-notice-${error.uncertain ? "uncertain" : "rejected"}`}><span>{error.message}</span></div>}

      {mode === "local" ? (
        <form className="identity-admin-form" onSubmit={(event) => { event.preventDefault(); void createLocal(); }}>
          <p>Creates a local sign-in account and generates its password, which is shown to you once. No email is sent. <strong>Creating the account does not grant access</strong>: {capabilities.identity_admin ? "you set access up as the next step." : "an access administrator must set access up afterwards."}</p>
          <div className="identity-admin-fields">
            <Input label="Username" value={username} required maxLength={128} autoComplete="off" onChange={(event) => setUsername(event.target.value)} />
            <Input label="Display name" value={displayName} required maxLength={256} autoComplete="off" onChange={(event) => setDisplayName(event.target.value)} />
            <Input label="Email (optional)" type="email" value={email} autoComplete="off" onChange={(event) => setEmail(event.target.value)} />
          </div>
          <div className="identity-admin-actions"><Button type="submit" variant="primary" disabled={busy || username.trim() === "" || displayName.trim() === ""}>Create account</Button><Button disabled={busy} onClick={onCancel}>Cancel</Button></div>
        </form>
      ) : (
        <form className="identity-admin-form" onSubmit={(event) => { event.preventDefault(); void prepareExternal(); }}>
          <p>Prepares ELSPETH access so the person is active the first time they sign in. It does not create an account with the sign-in provider and does not send an invitation.</p>
          <div className="identity-admin-fields">
            <label className="identity-admin-field">Sign-in provider
              <select className="input" value={provider} onChange={(event) => setProvider(event.target.value as (typeof SSO_PROVIDERS)[number])}>
                {SSO_PROVIDERS.map((value) => <option key={value} value={value}>{value}{value === configured ? " (this deployment)" : ""}</option>)}
              </select>
            </label>
            <Input label="Subject identifier" value={subject} required maxLength={512} autoComplete="off" hint="The identifier the provider sends for this person. It is often not their email address; ask whoever runs the provider if unsure." onChange={(event) => setSubject(event.target.value)} />
            <Input label="Organisation (optional)" value={organisation} maxLength={512} autoComplete="off" onChange={(event) => setOrganisation(event.target.value)} />
            <label className="identity-admin-field">Initial role
              <select className="input" value={role} onChange={(event) => setRole(event.target.value as ActivationRole)}>
                {ACTIVATION_ROLES.map((value) => <option key={value} value={value}>{value === "none" ? "No role yet" : ROLE_LABEL[value]}</option>)}
              </select>
            </label>
            <Input label="Note (required)" value={note} required maxLength={512} onChange={(event) => setNote(event.target.value)} />
          </div>
          <div className="identity-admin-actions"><Button type="submit" variant="primary" disabled={busy || subject.trim() === "" || note.trim() === ""}>Prepare access</Button><Button disabled={busy} onClick={onCancel}>Cancel</Button></div>
        </form>
      )}
    </section>
  );
}
