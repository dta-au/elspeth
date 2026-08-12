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
 * Passwords are server-generated and shown exactly once, in the banner at
 * the top of the dialog; they are never persisted client-side. Any new
 * action (or closing the dialog) discards the previous one.
 *
 * Modal chrome follows the ComposerPreferencesPanel/SecretsPanel
 * convention: backdrop + focus trap + Escape-close + role=dialog.
 */

interface UserAdminDialogProps {
  onClose: () => void;
  /** The signed-in admin's user_id — its row gets no delete button
   *  (the backend refuses self-deletion with a 400 anyway). */
  currentUserId: string;
}

export function UserAdminDialog({
  onClose,
  currentUserId,
}: UserAdminDialogProps): JSX.Element {
  const modalRef = useRef<HTMLDivElement>(null);
  useFocusTrap(modalRef, true);

  const [users, setUsers] = useState<AdminUserSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [generated, setGenerated] = useState<{
    user_id: string;
    password: string;
  } | null>(null);
  const [copied, setCopied] = useState(false);
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
      setCopied(false);
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
        setCopied(false);
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
      () => setCopied(true),
      () => setCopied(false),
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
          backgroundColor: "var(--color-surface, #fff)",
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
            style={{
              minWidth: 32,
              minHeight: 32,
              padding: 4,
              fontSize: 18,
              lineHeight: 1,
              cursor: "pointer",
            }}
          >
            ×
          </button>
        </div>
        <div className="secrets-panel-body" style={{ overflowY: "auto" }}>
          <p style={{ marginTop: 0, color: "var(--color-text-secondary)" }}>
            Dev-only account administration. Passwords are generated by the
            server and shown once — pass them on out-of-band.
          </p>

          {error !== null && (
            <div role="alert" className="composer-preferences-error">
              {error}
            </div>
          )}

          {generated !== null && (
            <div
              role="status"
              style={{
                border: "1px solid var(--color-border)",
                borderRadius: 6,
                padding: 12,
                marginBottom: 12,
              }}
            >
              <strong>
                One-time password for {generated.user_id}
              </strong>{" "}
              (not shown again):
              <div
                style={{
                  display: "flex",
                  gap: 8,
                  alignItems: "center",
                  marginTop: 6,
                }}
              >
                <code data-testid="generated-password">
                  {generated.password}
                </code>
                <button
                  type="button"
                  className="btn btn-compact"
                  onClick={onCopyPassword}
                >
                  {copied ? "Copied" : "Copy"}
                </button>
                <button
                  type="button"
                  className="btn btn-compact"
                  onClick={() => setGenerated(null)}
                >
                  Dismiss
                </button>
              </div>
            </div>
          )}

          {users === null ? (
            <p>Loading users…</p>
          ) : (
            <table style={{ width: "100%", borderCollapse: "collapse" }}>
              <thead>
                <tr style={{ textAlign: "left" }}>
                  <th>User</th>
                  <th>Display name</th>
                  <th>Email</th>
                  <th aria-label="actions" />
                </tr>
              </thead>
              <tbody>
                {users.map((user) => (
                  <tr key={user.user_id}>
                    <td>
                      <code>{user.user_id}</code>
                      {!user.email_verified && (
                        <span
                          title="Email not verified"
                          style={{ marginLeft: 4 }}
                        >
                          (unverified)
                        </span>
                      )}
                    </td>
                    <td>{user.display_name}</td>
                    <td>{user.email ?? "—"}</td>
                    <td style={{ whiteSpace: "nowrap", textAlign: "right" }}>
                      <button
                        type="button"
                        className="btn btn-compact"
                        disabled={busy}
                        onClick={() => onReset(user.user_id)}
                      >
                        Reset password
                      </button>{" "}
                      {user.user_id !== currentUserId &&
                        (confirmingDelete === user.user_id ? (
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
                            onClick={() => setConfirmingDelete(user.user_id)}
                          >
                            Delete
                          </button>
                        ))}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          )}

          <fieldset
            disabled={busy}
            style={{ marginTop: 16, border: "1px solid var(--color-border)" }}
          >
            <legend>Create user</legend>
            <div
              style={{
                display: "flex",
                gap: 8,
                flexWrap: "wrap",
                alignItems: "flex-end",
              }}
            >
              <label>
                Username
                <br />
                <input
                  type="text"
                  value={newUsername}
                  onChange={(e) => setNewUsername(e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label>
                Display name
                <br />
                <input
                  type="text"
                  value={newDisplayName}
                  onChange={(e) => setNewDisplayName(e.target.value)}
                  autoComplete="off"
                />
              </label>
              <label>
                Email (optional)
                <br />
                <input
                  type="text"
                  value={newEmail}
                  onChange={(e) => setNewEmail(e.target.value)}
                  autoComplete="off"
                />
              </label>
              <button
                type="button"
                className="btn btn-compact"
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
