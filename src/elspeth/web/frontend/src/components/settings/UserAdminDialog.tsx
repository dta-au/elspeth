import { useCallback, useEffect, useRef, useState } from "react";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import * as api from "@/api/client";
import type { AdminUserSummary } from "@/types/index";

/**
 * Dev-admin user management dialog (env-gated: reachable only when
 * /api/auth/me reports dev_admin, i.e. the backend's dev_admin_user names
 * the current local-auth user). List / create / delete accounts and reset
 * passwords for short-term dev deployments without an IdP.
 *
 * Passwords are server-generated and displayed in the banner at the top of
 * the dialog; they are never persisted client-side. Any new action (or
 * closing the dialog) discards the previous one. This is a dev/test
 * surface, not a production credential system — the generated password is
 * the account's standing password until an admin resets it again.
 *
 * After a successful create/reset, focus moves to the password banner so
 * keyboard focus never drops to <body> (the Create button disables while
 * focused) and the banner is scrolled into view.
 *
 * Modal chrome follows the SecretsPanel convention: backdrop + focus trap
 * + Escape-close + role=dialog, with the shared .secrets-panel-* shell
 * classes and .user-admin-* content classes from settings.css.
 */

interface UserAdminDialogProps {
  onClose: () => void;
  /** The signed-in admin's user_id — its row gets no delete button
   *  (the backend refuses self-deletion with a 400 anyway). */
  currentUserId: string;
}

type CopyState = "idle" | "copied" | "failed";

export function UserAdminDialog({
  onClose,
  currentUserId,
}: UserAdminDialogProps): JSX.Element {
  const modalRef = useRef<HTMLDivElement>(null);
  const bannerRef = useRef<HTMLDivElement>(null);
  useFocusTrap(modalRef, true);

  const [users, setUsers] = useState<AdminUserSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [generated, setGenerated] = useState<{
    user_id: string;
    password: string;
  } | null>(null);
  const [copyState, setCopyState] = useState<CopyState>("idle");
  const [confirmingDelete, setConfirmingDelete] = useState<string | null>(null);

  const [newUsername, setNewUsername] = useState("");
  const [newDisplayName, setNewDisplayName] = useState("");
  const [newEmail, setNewEmail] = useState("");

  useEffect(() => {
    function handleKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") onClose();
    }
    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  // Focus the freshly shown password banner: keeps keyboard focus alive
  // (the triggering button may have disabled) and scrolls it into view.
  useEffect(() => {
    if (generated !== null) bannerRef.current?.focus();
  }, [generated]);

  const reload = useCallback(async () => {
    try {
      const { users: rows } = await api.fetchAdminUsers();
      setUsers(rows);
      setError(null);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Failed to load users");
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const run = useCallback(
    async (action: () => Promise<void>) => {
      setBusy(true);
      setError(null);
      setConfirmingDelete(null);
      try {
        await action();
        await reload();
      } catch (err) {
        setError(err instanceof Error ? err.message : "Action failed");
      } finally {
        setBusy(false);
      }
    },
    [reload],
  );

  const onCreate = useCallback(() => {
    const username = newUsername.trim();
    const displayName = newDisplayName.trim();
    const email = newEmail.trim();
    void run(async () => {
      const result = await api.createAdminUser({
        username,
        display_name: displayName,
        ...(email !== "" ? { email } : {}),
      });
      setGenerated(result);
      setCopyState("idle");
      setNewUsername("");
      setNewDisplayName("");
      setNewEmail("");
    });
  }, [newDisplayName, newEmail, newUsername, run]);

  const onReset = useCallback(
    (userId: string) => {
      void run(async () => {
        const result = await api.resetAdminUserPassword(userId);
        setGenerated(result);
        setCopyState("idle");
      });
    },
    [run],
  );

  const onDelete = useCallback(
    (userId: string) => {
      void run(async () => {
        await api.deleteAdminUser(userId);
        setGenerated(null);
      });
    },
    [run],
  );

  const onCopyPassword = useCallback(() => {
    if (generated === null) return;
    void navigator.clipboard.writeText(generated.password).then(
      () => setCopyState("copied"),
      () => setCopyState("failed"),
    );
  }, [generated]);

  const createDisabled =
    busy || newUsername.trim() === "" || newDisplayName.trim() === "";

  return (
    <>
      {/* Backdrop */}
      <div
        role="presentation"
        onClick={onClose}
        style={{
          position: "fixed",
          inset: 0,
          backgroundColor: "rgba(0,0,0,0.45)",
          zIndex: 100,
        }}
      />
      {/* Modal */}
      <div
        ref={modalRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="user-admin-title"
        style={{
          position: "fixed",
          top: "50%",
          left: "50%",
          transform: "translate(-50%, -50%)",
          zIndex: 101,
          width: 560,
          maxWidth: "calc(100vw - 32px)",
          maxHeight: "calc(100vh - 64px)",
          display: "flex",
          flexDirection: "column",
          backgroundColor: "var(--color-surface)",
          borderRadius: 8,
          boxShadow: "0 8px 32px rgba(0,0,0,0.25)",
          border: "1px solid var(--color-border)",
          fontSize: 13,
          overflow: "hidden",
        }}
      >
        <div className="secrets-panel-header">
          <h2 id="user-admin-title" className="secrets-panel-title">
            User management
          </h2>
          <button
            type="button"
            onClick={onClose}
            aria-label="Close user management dialog"
            className="secrets-panel-close"
          >
            ×
          </button>
        </div>
        <div className="secrets-panel-body">
          <p className="user-admin-intro">
            Dev-only account administration. Passwords are generated by the
            server and shown here until the next action — pass them on
            out-of-band.
          </p>

          {error !== null && (
            <div role="alert" className="composer-preferences-error">
              {error}
            </div>
          )}

          {generated !== null && (
            <div
              role="status"
              ref={bannerRef}
              tabIndex={-1}
              className="user-admin-password-banner"
            >
              <strong>Generated password for {generated.user_id}</strong>:
              <div className="user-admin-password-row">
                <code
                  data-testid="generated-password"
                  className="user-admin-password-value"
                >
                  {generated.password}
                </code>
                <button
                  type="button"
                  className="btn btn-compact"
                  onClick={onCopyPassword}
                >
                  {copyState === "copied" ? "Copied" : "Copy"}
                </button>
                <button
                  type="button"
                  className="btn btn-compact"
                  onClick={() => setGenerated(null)}
                >
                  Dismiss
                </button>
                {copyState === "failed" && (
                  <span role="alert" className="user-admin-copy-failed">
                    Copy failed — select the text manually.
                  </span>
                )}
              </div>
            </div>
          )}

          {users === null ? (
            <p className="user-admin-empty">Loading users…</p>
          ) : users.length === 0 ? (
            <p className="user-admin-empty">No accounts yet.</p>
          ) : (
            <table className="user-admin-table">
              <thead>
                <tr>
                  <th scope="col">User</th>
                  <th scope="col">Display name</th>
                  <th scope="col">Email</th>
                  <th scope="col">
                    <span className="sr-only">Actions</span>
                  </th>
                </tr>
              </thead>
              <tbody>
                {users.map((user) => (
                  <tr key={user.user_id}>
                    <td>
                      <code className="user-admin-username">
                        {user.user_id}
                      </code>
                      {!user.email_verified && (
                        <span
                          title="Email not verified"
                          className="user-admin-unverified"
                        >
                          (unverified)
                        </span>
                      )}
                    </td>
                    <td>{user.display_name}</td>
                    <td>{user.email ?? "—"}</td>
                    <td className="user-admin-row-actions">
                      <div className="user-admin-row-action-group">
                        <button
                          type="button"
                          className="btn btn-compact"
                          disabled={busy}
                          onClick={() => onReset(user.user_id)}
                        >
                          Reset password
                        </button>
                        {user.user_id !== currentUserId && (
                          // The delete control shares a grid cell with a
                          // hidden sizer carrying the longest label it can
                          // take (elspeth-a0700fefff). The cell is
                          // right-aligned and nowrap, so before this the
                          // "Delete" → "Confirm delete" swap dragged both
                          // buttons left out from under the pointer at the
                          // moment the user was asked to confirm an
                          // irreversible action.
                          <span className="user-admin-delete-slot">
                            <span
                              aria-hidden="true"
                              className="btn btn-compact user-admin-delete-sizer"
                            >
                              Confirm delete
                            </span>
                            {confirmingDelete === user.user_id ? (
                              <button
                                type="button"
                                className="btn btn-compact"
                                disabled={busy}
                                onClick={() => onDelete(user.user_id)}
                              >
                                Confirm delete
                              </button>
                            ) : (
                              <button
                                type="button"
                                className="btn btn-compact"
                                disabled={busy}
                                onClick={() =>
                                  setConfirmingDelete(user.user_id)
                                }
                              >
                                Delete
                              </button>
                            )}
                          </span>
                        )}
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <fieldset disabled={busy} className="user-admin-create-form">
            <legend>Create user</legend>
            <div className="user-admin-create-fields">
              <label className="user-admin-field">
                <span className="field-label">Username</span>
                <input
                  type="text"
                  className="input"
                  value={newUsername}
                  onChange={(e) => setNewUsername(e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label className="user-admin-field">
                <span className="field-label">Display name</span>
                <input
                  type="text"
                  className="input"
                  value={newDisplayName}
                  onChange={(e) => setNewDisplayName(e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label className="user-admin-field">
                <span className="field-label">Email (optional)</span>
                <input
                  type="email"
                  className="input"
                  value={newEmail}
                  onChange={(e) => setNewEmail(e.target.value)}
                  autoComplete="off"
                />
              </label>
              {/* .btn, not .btn-compact: the fields in this row are the shared
                  .input primitive on the --size-control rung, and the row is
                  align-items: flex-end, so a 36px button next to a 44px field
                  shows the height difference as a step at the top edge
                  (elspeth-2580a7b094). */}
              <button
                type="button"
                className="btn"
                disabled={createDisabled}
                onClick={onCreate}
              >
                Create
              </button>
            </div>
          </fieldset>
        </div>
      </div>
    </>
  );
}
