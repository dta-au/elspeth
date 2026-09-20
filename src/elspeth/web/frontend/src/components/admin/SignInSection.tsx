import { useCallback, useState } from "react";
import * as api from "@/api/client";
import { PROVIDER_LABEL, errorStatus } from "@/api/people";
import { Button, Input } from "@/components/ui";
import type { IdentityProvider } from "@/types/identityAdmin";
import type { LocalAccountView } from "@/types/people";
import type { GeneratedCredential } from "./GeneratedPassword";
import { MutationNoticeView } from "./MutationNoticeView";
import { useCancelToTrigger, usePersonMutation, useSubview } from "./peoplePanel";

type Confirm = "reset" | "delete" | null;

interface Props {
  personName: string;
  /** How this person signs in when they have no local account the caller may manage. */
  provider: IdentityProvider;
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
export function SignInSection({ personName, provider, account, identityRetired, canManage, isSelf, onCredential, onChanged, onGone }: Props): JSX.Element {
  const [confirm, setConfirm] = useState<Confirm>(null);
  // The account this section last tried to delete. Deletion is two writes in
  // two stores; when only the first lands, the account is gone and the person
  // is still live, and finishing the job needs the name that is no longer shown.
  const [deleting, setDeleting] = useState<string | null>(null);
  // The reason typed for that deletion, held BESIDE the name: when only the
  // first write landed, the retry writes the one audit row the deletion ever
  // gets, so it must carry the reason already given. The field below starts
  // from it, and is the ask-again fallback when none is held (a reload).
  const [reason, setReason] = useState("");
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
  const cancelDelete = useCallback(() => { setConfirm(null); setReason(""); }, []);
  const { triggerRef, cancel } = useCancelToTrigger<HTMLButtonElement>(confirm === "delete" ? cancelDelete : close);
  useSubview(`signin:${account?.username ?? provider}`, confirm !== null, confirm === "delete" && reason.trim() !== "", cancel);
  const blocked = mutation.busy || mutation.mustReconcile || deletion.busy || deletion.mustReconcile;

  if (account === null || !canManage) {
    const unfinished = account === null && deleting !== null && identityRetired === false;
    return (
      <section aria-label={`Sign-in for ${personName}`} className="people-section">
        <MutationNoticeView mutation={deletion} />
        {unfinished && (
          <div role="alert" className="people-notice people-notice-uncertain">
            <span>The local account <code>{deleting}</code> was deleted, but retiring {personName} did not finish, so they still hold their access and roles. Finish the removal.</span>
            <form className="identity-admin-form" onSubmit={(event) => {
              event.preventDefault();
              void deletion.run(() => api.deleteAdminUser(deleting, reason.trim()), `Finished removing ${personName}.`, "The removal was not finished");
            }}>
            <Input label="Reason (required)" value={reason} maxLength={api.DELETE_ADMIN_USER_REASON_MAX_LENGTH} required hint="Recorded with the removal. This is the reason you gave; change it only if it was wrong." onChange={(event) => setReason(event.target.value)} />
            <Button compact type="submit" variant="danger" disabled={deletion.busy || reason.trim() === ""}>Finish removing {personName}</Button>
            </form>
          </div>
        )}
        <p>{provider === "local"
          ? `${personName} signs in with a local account. Local accounts are managed by the local account administrator.`
          : provider === "service"
            ? `${personName} is a service account. Its credentials are managed by the operator, not in ELSPETH.`
            : `${personName} signs in through ${PROVIDER_LABEL[provider]}. Passwords and account details are managed there, not in ELSPETH.`}</p>
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
          <h5>Reset the password for {personName}?</h5>
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
        <form className="identity-admin-form" onSubmit={(event) => {
          event.preventDefault();
          const username = account.username;
          setDeleting(username);
          void deletion.run(() => api.deleteAdminUser(username, reason.trim()), `Deleted the local account for ${personName}.`, "Account was not deleted").then((ok) => { if (ok) close(); });
        }}>
          <h5>Delete the local account for {personName}?</h5>
          <p>This is not the same as disabling access, and it cannot be undone:</p>
          <ul>
            <li>The local password and sign-in for <code>{account.username}</code> are removed.</li>
            <li>The identity is retired. Its history is kept for the audit record.</li>
            <li>A later account named <code>{account.username}</code> starts fresh. It does not receive this person's roles, limits or history.</li>
          </ul>
          <p>To stop someone signing in while keeping their account, use <strong>Disable access</strong> instead.</p>
          {/* Deleting is the graver act; it must not cost less than disabling,
              which asks for a reason (ruling D3). Recorded in the audit trail. */}
          <Input label="Reason (required)" value={reason} maxLength={api.DELETE_ADMIN_USER_REASON_MAX_LENGTH} required onChange={(event) => setReason(event.target.value)} />
          <div className="identity-admin-actions">
            <Button type="submit" variant="danger" disabled={blocked || reason.trim() === ""}>Delete local account</Button>
            <Button disabled={deletion.busy} onClick={cancel}>Cancel</Button>
          </div>
        </form>
      )}
    </section>
  );
}
