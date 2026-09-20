import { useCallback, useState } from "react";
import * as api from "@/api/client";
import { errorStatus } from "@/api/people";
import { Button } from "@/components/ui";
import type { LocalAccountView } from "@/types/people";
import type { GeneratedCredential } from "./GeneratedPassword";
import { MutationNoticeView } from "./MutationNoticeView";
import { useCancelToTrigger, usePersonMutation, useSubview } from "./peoplePanel";

type Confirm = "reset" | "delete" | null;

interface Props {
  personName: string;
  /** How this person signs in when they have no local account the caller may manage. */
  providerLabel: string;
  account: LocalAccountView | null;
  /** Whether the identity behind the account is retired; null when the caller cannot see identities. */
  identityRetired: boolean | null;
  canManage: boolean;
  isSelf: boolean;
  onCredential: (credential: GeneratedCredential) => void;
  /** Re-read this person. Rejects with the read's error, 404 included. */
  onChanged: () => Promise<void>;
  /** The re-read found nobody under this key any more. */
  onGone: () => void;
}

/**
 * Sign-in controls. Credentials belong to a different capability than access,
 * so this section renders an explanation rather than controls when the
 * caller lacks it, and it never describes deletion as a kind of disabling.
 */
export function SignInSection({ personName, providerLabel, account, identityRetired, canManage, isSelf, onCredential, onChanged, onGone }: Props): JSX.Element {
  const [confirm, setConfirm] = useState<Confirm>(null);
  // The account this section last tried to delete. Deletion is two writes in
  // two stores; when only the first lands, the account is gone and the person
  // is still live, and finishing the job needs the name that is no longer shown.
  const [deleting, setDeleting] = useState<string | null>(null);
  const mutation = usePersonMutation(onChanged);
  // The same re-read serves a confirmed deletion and the CHECK after an
  // unanswered one, so it has to look: a person who is no longer there under
  // this key is the deletion having landed, not a failed refresh.
  const rereadAfterDeletion = useCallback(async () => {
    try {
      await onChanged();
    } catch (error) {
      if (errorStatus(error) !== 404) throw error;
      onGone();
    }
  }, [onChanged, onGone]);
  const deletion = usePersonMutation(rereadAfterDeletion);
  const close = useCallback(() => setConfirm(null), []);
  const { triggerRef, cancel } = useCancelToTrigger<HTMLButtonElement>(close);
  useSubview(`signin:${account?.username ?? providerLabel}`, confirm !== null, false, cancel);
  const blocked = mutation.busy || mutation.mustReconcile || deletion.busy || deletion.mustReconcile;

  if (account === null || !canManage) {
    const unfinished = account === null && deleting !== null && identityRetired === false;
    return (
      <section aria-label={`Sign-in for ${personName}`} className="people-section">
        <MutationNoticeView mutation={deletion} />
        {unfinished && (
          <div role="alert" className="people-notice people-notice-uncertain">
            <span>The local account <code>{deleting}</code> was deleted, but retiring {personName} did not finish, so they still hold their access and roles. Finish the removal.</span>
            <Button compact variant="danger" disabled={deletion.busy} onClick={() => { void deletion.run(() => api.deleteAdminUser(deleting), `Finished removing ${personName}.`, "The removal was not finished"); }}>Finish removing {personName}</Button>
          </div>
        )}
        <p>{providerLabel === "local"
          ? `${personName} signs in with a local account. Local accounts are managed by the local account administrator.`
          : `${personName} signs in through ${providerLabel}. Passwords and account details are managed there, not in ELSPETH.`}</p>
      </section>
    );
  }

  return (
    <section aria-label={`Sign-in for ${personName}`} className="people-section">
      <p>Local account <code>{account.username}</code>{account.email !== null && <> · {account.email}{!account.email_verified && " (unverified)"}</>}. Local accounts are for development deployments without a sign-in provider.</p>
      <MutationNoticeView mutation={mutation} />
      <MutationNoticeView mutation={deletion} />
      {mutation.notice?.kind === "uncertain" && <p className="people-grant-purpose">If a password was reset, it cannot be shown again. Reset it once more to get a new one.</p>}
      {confirm === null && (
        <div className="identity-admin-actions">
          <Button ref={triggerRef} disabled={blocked} aria-label={`Reset password for ${personName}`} onClick={() => { mutation.clear(); setConfirm("reset"); }}>Reset password</Button>
          {!isSelf && <Button variant="danger" disabled={blocked} aria-label={`Delete local account for ${personName}`} onClick={() => { mutation.clear(); setConfirm("delete"); }}>Delete local account</Button>}
        </div>
      )}
      {isSelf && confirm === null && <p className="people-grant-purpose">You cannot delete the account you are signed in with.</p>}

      {confirm === "reset" && (
        <div className="identity-admin-form">
          <h4>Reset the password for {personName}?</h4>
          <p>A new password is generated and shown to you once. The current password stops working. Sessions that are already signed in stay signed in until they expire.</p>
          <div className="identity-admin-actions">
            <Button variant="primary" disabled={blocked} onClick={() => {
              const username = account.username;
              let credential: GeneratedCredential | null = null;
              void mutation.run(async () => { const result = await api.resetAdminUserPassword(username); credential = { username: result.user_id, password: result.password, cause: "reset" }; }, `Password reset for ${personName}.`, "Password was not reset")
                .then((ok) => { if (credential !== null) onCredential(credential); if (ok || credential !== null) close(); });
            }}>Reset password</Button>
            <Button disabled={mutation.busy} onClick={cancel}>Cancel</Button>
          </div>
        </div>
      )}

      {confirm === "delete" && (
        <div className="identity-admin-form">
          <h4>Delete the local account for {personName}?</h4>
          <p>This is not the same as disabling access, and it cannot be undone:</p>
          <ul>
            <li>The local password and sign-in for <code>{account.username}</code> are removed.</li>
            <li>The identity is retired. Its history is kept for the audit record.</li>
            <li>A later account named <code>{account.username}</code> starts fresh. It does not receive this person's roles, limits or history.</li>
          </ul>
          <p>To stop someone signing in while keeping their account, use <strong>Disable access</strong> instead.</p>
          <div className="identity-admin-actions">
            <Button variant="danger" disabled={blocked} onClick={() => {
              const username = account.username;
              setDeleting(username);
              void deletion.run(() => api.deleteAdminUser(username), `Deleted the local account for ${personName}.`, "Account was not deleted").then((ok) => { if (ok) close(); });
            }}>Delete local account</Button>
            <Button disabled={deletion.busy} onClick={cancel}>Cancel</Button>
          </div>
        </div>
      )}
    </section>
  );
}
