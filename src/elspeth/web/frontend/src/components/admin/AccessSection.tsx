import { useCallback, useEffect, useState } from "react";
import * as admin from "@/api/identityAdmin";
import { Button, Input } from "@/components/ui";
import type { ActivationRole, IdentityRole, RoleView } from "@/types/identityAdmin";
import type { IdentityPerson } from "@/types/people";
import { MutationNoticeView } from "./MutationNoticeView";
import { ROLE_LABEL, formatInstant } from "./peopleFormat";
import { useCancelToTrigger, usePersonMutation, useSubview } from "./peoplePanel";

const ACTIVATION_ROLES: ActivationRole[] = ["user", "approver", "reviewer", "none"];

type Action = "approve" | "enable" | "disable";

interface Props {
  person: IdentityPerson;
  personName: string;
  /** Re-read the person. The panel keeps showing them even when their new state no longer matches the list filter. */
  onChanged: () => Promise<void>;
}

const STATE_LABEL = { pending: "Pending access", active: "Active", disabled: "Disabled" } as const;

/** Status always visible; one reversible action at a time, confirmed inline with the person named. */
export function AccessSection({ person, personName, onChanged }: Props): JSX.Element {
  const { identity } = person;
  const [action, setAction] = useState<Action | null>(null);
  const [text, setText] = useState("");
  const [role, setRole] = useState<ActivationRole>("user");
  const [retained, setRetained] = useState<RoleView[] | "unavailable" | null>(null);
  const mutation = usePersonMutation(onChanged);
  const close = useCallback(() => { setAction(null); setText(""); }, []);
  const { triggerRef, cancel } = useCancelToTrigger<HTMLButtonElement>(close);
  useSubview(`access:${identity.identity_id}`, action !== null, text.trim() !== "", cancel);

  // A returning (re-pended) person may still hold earlier grants, and an
  // approval that adds nothing can still return an administrator. Show them
  // BEFORE the confirmation, not after.
  const returning = identity.access_state === "pending" && identity.activated_at !== null;
  useEffect(() => {
    if (action !== "approve" || !returning) { setRetained(null); return; }
    let active = true;
    void admin.listRoles(identity.identity_id).then(
      (result) => { if (active) setRetained(result.roles); },
      () => { if (active) setRetained("unavailable"); },
    );
    return () => { active = false; };
  }, [action, returning, identity.identity_id]);

  const available: Action | null = person.retired ? null : identity.access_state === "pending" ? "approve" : identity.access_state === "disabled" ? "enable" : "disable";
  const selfBlocked = available === "disable" && person.actions.is_self;
  const blocked = mutation.busy || mutation.mustReconcile;
  const buttonLabel = { approve: "Approve access", enable: "Enable access", disable: "Disable access" } as const;

  return (
    <section aria-label={`Access for ${personName}`} className="people-section">
      <div className="people-access-status">
        <span className={`people-state people-state-${person.retired ? "retired" : identity.access_state}`}>{person.retired ? "Retired account" : STATE_LABEL[identity.access_state]}</span>
        <span className="people-grant-meta">
          {person.retired ? "This is the history of a deleted local account. It cannot sign in and is kept for the audit record."
            : identity.access_state === "disabled" ? `Disabled${identity.disabled_at !== null ? ` ${formatInstant(identity.disabled_at)}` : ""}${identity.disable_reason !== null ? `. Reason: ${identity.disable_reason}` : ""}`
            : identity.access_state === "pending" ? (returning ? "Returning after a period away. Approve to restore access." : identity.pre_provisioned_at !== null ? "Prepared in advance; not yet active." : "Signed in and is waiting for approval.")
            : `Active${identity.activated_at !== null ? ` since ${formatInstant(identity.activated_at)}` : ""}.`}
        </span>
        {available !== null && action === null && (
          <Button ref={triggerRef} variant={available === "disable" ? "danger" : "primary"} disabled={blocked || selfBlocked} onClick={() => { mutation.clear(); setAction(available); }}>{buttonLabel[available]}</Button>
        )}
      </div>
      {selfBlocked && <p className="people-grant-purpose">You cannot disable your own access. Another administrator can do it.</p>}
      {/* Said on the one person it is about, as a fact (the server names the
          sole administrator per person) — not as "if" on everyone. */}
      {available === "disable" && !selfBlocked && person.sole_active_admin && <p className="people-grant-purpose">{personName} is the only active administrator, so disabling them is refused. Add another administrator first.</p>}
      <MutationNoticeView mutation={mutation} />

      {action !== null && (
        <form className="identity-admin-form" onSubmit={(event) => {
          event.preventDefault();
          const id = identity.identity_id;
          const value = text.trim();
          const [write, done, failed] = action === "approve"
            ? [() => admin.activateIdentity(id, role, value), `Approved access for ${personName}.`, "Access was not approved"] as const
            : action === "enable"
              ? [() => admin.enableIdentity(id, value), `Enabled access for ${personName}.`, "Access was not enabled"] as const
              : [() => admin.disableIdentity(id, value), `Disabled access for ${personName}.`, "Access was not disabled"] as const;
          void mutation.run(write, done, failed).then((ok) => { if (ok) close(); });
        }}>
          <h5>{buttonLabel[action]} for {personName}?</h5>
          {action === "approve" && (
            <>
              {returning && (
                <div className="people-grant-purpose">
                  {retained === null ? "Checking the roles this person already holds…"
                    : retained === "unavailable" ? "The roles this person already holds could not be read. Approving may restore roles you cannot see here."
                    : retained.length === 0 ? "This person holds no roles from before."
                    : <>Approving restores the roles {personName} already holds: <strong>{retained.map((grant) => ROLE_LABEL[grant.role as IdentityRole]).join(", ")}</strong>.</>}
                </div>
              )}
              <label className="identity-admin-field">{returning ? "Role to add" : "Initial role"}
                <select className="input" value={role} onChange={(event) => setRole(event.target.value as ActivationRole)}>
                  {ACTIVATION_ROLES.map((value) => <option key={value} value={value}>{value === "none" ? "Add no new role" : ROLE_LABEL[value]}</option>)}
                </select>
              </label>
            </>
          )}
          {action === "disable" && <p>{personName} will be signed out of new requests and cannot sign in. Approver links to and from them are removed and are <strong>not</strong> restored if you enable access again.</p>}
          {action === "enable" && <p>{personName} can sign in again. Approver links removed when they were disabled are not restored.</p>}
          <Input label={action === "disable" ? "Reason (required)" : "Note (required)"} value={text} maxLength={512} required onChange={(event) => setText(event.target.value)} />
          <div className="identity-admin-actions">
            <Button type="submit" variant={action === "disable" ? "danger" : "primary"} disabled={blocked || text.trim() === ""}>{buttonLabel[action]}</Button>
            <Button disabled={mutation.busy} onClick={cancel}>Cancel</Button>
          </div>
        </form>
      )}
    </section>
  );
}
