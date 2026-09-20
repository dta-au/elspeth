import { useCallback, useEffect, useId, useState } from "react";
import * as admin from "@/api/identityAdmin";
import { isAbort } from "@/api/people";
import { Button, Input } from "@/components/ui";
import type { IdentityRole, RoleView } from "@/types/identityAdmin";
import { MutationNoticeView } from "./MutationNoticeView";
import { ROLE_LABEL, ROLE_PURPOSE, formatInstant, localInputToUtcIso, roleConflictAdvice, viewerTimeZone } from "./peopleFormat";
import { useCancelToTrigger, usePersonMutation, useSubview } from "./peoplePanel";

const ROLES: IdentityRole[] = ["user", "approver", "reviewer", "curator", "auditor", "oversight", "admin"];

type Form = { type: "grant" } | { type: "revoke"; grant: RoleView } | null;

interface Props {
  identityId: string;
  personName: string;
  kind: "human" | "service";
  /** A role write landed. The administrator count the panel warns about may have moved. */
  onChanged: () => void;
}

/**
 * One person's role grants. The identity is bound by the panel: nobody types
 * an identity ID here. Roles are a SET of grants, each revoked on its own;
 * there is deliberately no "change role" control, because a swap is two
 * writes with two outcomes and one dropdown would hide the second.
 */
export function RolesEditor({ identityId, personName, kind, onChanged }: Props): JSX.Element {
  const [grants, setGrants] = useState<RoleView[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [form, setForm] = useState<Form>(null);
  const [role, setRole] = useState<IdentityRole>(kind === "service" ? "oversight" : "user");
  const [expiry, setExpiry] = useState("");
  const [note, setNote] = useState("");
  const expiryHintId = useId();
  const adviceId = useId();

  const reload = useCallback(async () => {
    const result = await admin.listRoles(identityId);
    setGrants(result.roles);
    setLoadError(null);
    onChanged();
  }, [identityId, onChanged]);

  const load = useCallback(() => {
    let active = true;
    setGrants(null);
    setLoadError(null);
    void admin.listRoles(identityId).then(
      (result) => { if (active) setGrants(result.roles); },
      (error: unknown) => { if (active && !isAbort(error)) setLoadError(admin.adminErrorMessage(error, "Could not load roles")); },
    );
    return () => { active = false; };
  }, [identityId]);
  useEffect(() => load(), [load]);

  const mutation = usePersonMutation(reload);
  const closeForm = useCallback(() => { setForm(null); setExpiry(""); setNote(""); }, []);
  const { triggerRef, cancel } = useCancelToTrigger<HTMLButtonElement>(closeForm);
  useSubview(`roles:${identityId}`, form !== null, form?.type === "grant" && (expiry !== "" || note.trim() !== ""), cancel);

  const held = (grants ?? []).filter((grant) => grant.scope === null).map((grant) => grant.role);
  const advice = roleConflictAdvice(role, held, kind);
  const expiryIso = expiry === "" ? null : localInputToUtcIso(expiry);
  const expiryInPast = expiryIso !== null && new Date(expiryIso).getTime() <= Date.now();
  const blocked = mutation.busy || mutation.mustReconcile;

  return (
    <section aria-label={`Roles for ${personName}`} className="people-section">
      <MutationNoticeView mutation={mutation} />
      {loadError !== null ? (
        <div role="alert" className="people-notice people-notice-rejected"><span>{loadError}</span><Button compact onClick={() => load()}>Retry</Button></div>
      ) : grants === null ? <p>Loading roles…</p> : grants.length === 0 ? (
        <p>{personName} holds no roles.</p>
      ) : (
        <ul className="people-grants">
          {grants.map((grant) => (
            <li key={grant.role_id} className="people-grant">
              <div>
                <strong>{ROLE_LABEL[grant.role]}</strong>
                <span className="people-grant-meta"> · {grant.scope === null ? "Deployment-wide" : `Scope: ${grant.scope}`} · {grant.expires_at === null ? "No expiry" : `Expires ${formatInstant(grant.expires_at)}`}</span>
                <p className="people-grant-purpose">{ROLE_PURPOSE[grant.role]}</p>
              </div>
              <Button compact disabled={blocked || form !== null} aria-label={`Revoke ${ROLE_LABEL[grant.role]} from ${personName}`} onClick={() => { mutation.clear(); setForm({ type: "revoke", grant }); }}>Revoke</Button>
            </li>
          ))}
        </ul>
      )}

      {form === null && grants !== null && <div><Button ref={triggerRef} disabled={blocked} onClick={() => { mutation.clear(); setForm({ type: "grant" }); }}>Add role</Button></div>}

      {form?.type === "revoke" && (
        <form className="identity-admin-form" onSubmit={(event) => {
          event.preventDefault();
          const target = form.grant;
          void mutation.run(() => admin.revokeRole(target.role_id, note.trim() || undefined), `Revoked ${ROLE_LABEL[target.role]} from ${personName}.`, "Role was not revoked").then((ok) => { if (ok) closeForm(); });
        }}>
          <h4>Revoke {ROLE_LABEL[form.grant.role]} from {personName}?</h4>
          <p>This removes one grant. {personName}'s other roles are not changed.</p>
          <Input label="Note (optional)" value={note} maxLength={512} onChange={(event) => setNote(event.target.value)} />
          <div className="identity-admin-actions"><Button type="submit" variant="danger" disabled={blocked}>Revoke role</Button><Button disabled={mutation.busy} onClick={cancel}>Cancel</Button></div>
        </form>
      )}

      {form?.type === "grant" && (
        <form className="identity-admin-form" onSubmit={(event) => {
          event.preventDefault();
          if (expiryInPast || (expiry !== "" && expiryIso === null)) return;
          void mutation.run(
            () => admin.grantRole({ identity_id: identityId, role, ...(expiryIso !== null ? { expires_at: expiryIso } : {}), ...(note.trim() ? { note: note.trim() } : {}) }),
            `Granted ${ROLE_LABEL[role]} to ${personName}.`,
            "Role was not granted",
          ).then((ok) => { if (ok) closeForm(); });
        }}>
          <h4>Add a deployment-wide role for {personName}</h4>
          <div className="identity-admin-fields">
            <label className="identity-admin-field">Role
              <select className="input" value={role} aria-describedby={adviceId} onChange={(event) => setRole(event.target.value as IdentityRole)}>
                {ROLES.map((value) => <option key={value} value={value}>{ROLE_LABEL[value]}</option>)}
              </select>
            </label>
            <Input label="Expires (optional)" type="datetime-local" value={expiry} aria-describedby={expiryHintId} aria-invalid={expiryInPast} onChange={(event) => setExpiry(event.target.value)} />
            <Input label="Note (optional)" value={note} maxLength={512} onChange={(event) => setNote(event.target.value)} />
          </div>
          <p id={adviceId} className="people-grant-purpose">{ROLE_PURPOSE[role]}{advice !== null && <strong> {advice}</strong>}</p>
          <p id={expiryHintId} className="people-grant-purpose">{expiryInPast ? "The expiry must be in the future. " : ""}Expiry is entered in your time zone ({viewerTimeZone()}). Leave it empty for a grant that does not expire.</p>
          <div className="identity-admin-actions"><Button type="submit" variant="primary" disabled={blocked || expiryInPast}>Grant role</Button><Button disabled={mutation.busy} onClick={cancel}>Cancel</Button></div>
        </form>
      )}
    </section>
  );
}
