/**
 * SaveForReviewDialog — confirmation + share-URL display (Phase 6B Task 4).
 *
 * Mounted by App.tsx (top-level) and reads `dialogOpen`, `latestResponse`,
 * `inFlight`, `error`, and the `request` phase from `useShareableReviewStore`.
 * The dialog has four observable states:
 *
 *   * request.kind === "configure" → the configure step: may recipients import
 *     a copy (with the files that would travel), and how long the link lives.
 *     Nothing is minted until "Create link".
 *   * inFlight=true  → "Minting share link..." spinner.
 *   * error set      → error banner with a "Try again" affordance.
 *   * latestResponse → share URL + expiry + copy-to-clipboard button.
 *
 * The share_url returned from the backend is path-only (`/#/shared/{token}`);
 * the dialog prepends `location.origin` for the copy-to-clipboard
 * affordance and the "Open in new tab" link so the user always gets an
 * absolute URL.
 *
 * Copy-to-clipboard uses `navigator.clipboard.writeText`. The plan calls
 * out that environments without Clipboard API support fall back to
 * selectable text — the input element used to display the URL IS
 * selectable, so users on locked-down browsers can still copy manually.
 */

import { type JSX, useEffect, useRef, useState } from "react";

import { Button, Input } from "@/components/ui";
import type { ShareLifetimeSeconds, ShareableFilesResponse } from "@/types/api";
import { formatBytes } from "@/utils/bytes";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { useExecutionStore } from "@/stores/executionStore";
import { useShareableReviewStore } from "@/stores/shareableReviewStore";

const COPY_FEEDBACK_TIMEOUT_MS = 2000;

const LIFETIME_CHOICES: ReadonlyArray<{ seconds: ShareLifetimeSeconds; label: string }> = [
  { seconds: 86400, label: "1 day" },
  { seconds: 604800, label: "7 days" },
  { seconds: 2592000, label: "30 days" },
];
const DEFAULT_LIFETIME_SECONDS: ShareLifetimeSeconds = 2592000;

interface ConfigureStepProps {
  files: ShareableFilesResponse | null;
  filesError: string | null;
  createDisabled: boolean;
  createDisabledTitle: string | undefined;
  onCreate: (allowImport: boolean, lifetimeSeconds: ShareLifetimeSeconds) => void;
  onReloadFiles: () => void;
}

/** The configure step. Mounted only while the request is in the configure
 *  phase, so its choices reset to the defaults each time the dialog opens. */
function ConfigureStep({
  files,
  filesError,
  createDisabled,
  createDisabledTitle,
  onCreate,
  onReloadFiles,
}: ConfigureStepProps): JSX.Element {
  const [allowImport, setAllowImport] = useState(false);
  const [lifetime, setLifetime] = useState<ShareLifetimeSeconds>(DEFAULT_LIFETIME_SECONDS);

  const loading = files === null && filesError === null;
  const importable = files !== null && files.importable;
  const importChecked = importable && allowImport;

  let importNote: string | null = null;
  if (filesError !== null) {
    importNote = "Recipients cannot import a copy because the file list could not be loaded. You can still create a view-only link.";
  } else if (files !== null && !files.importable) {
    importNote = files.not_importable_reason ?? "This pipeline cannot be imported by recipients.";
  }

  return (
    <div className="save-for-review-configure" data-testid="save-for-review-configure">
      <div className="save-for-review-option">
        <label className="save-for-review-option-label">
          <Input
            type="checkbox"
            checked={importChecked}
            disabled={!importable}
            aria-describedby={importNote !== null ? "save-for-review-import-note" : undefined}
            onChange={(e) => setAllowImport(e.currentTarget.checked)}
            data-testid="save-for-review-import-checkbox"
          />
          <span>Let recipients import a copy</span>
        </label>
        {loading && (
          <p role="status" className="save-for-review-tip" data-testid="save-for-review-files-loading">
            Loading the files this link would share...
          </p>
        )}
        {importNote !== null && (
          <p id="save-for-review-import-note" className="save-for-review-tip" data-testid="save-for-review-import-note">
            {importNote}
          </p>
        )}
        {filesError !== null && (
          <div role="alert" className="save-for-review-error" data-testid="save-for-review-files-error">
            <p>{filesError}</p>
            <Button compact type="button" onClick={onReloadFiles} data-testid="save-for-review-files-retry">
              Reload files
            </Button>
          </div>
        )}
        {importChecked && files !== null && (
          <div data-testid="save-for-review-file-list">
            {files.files.length === 0 ? (
              <p className="save-for-review-tip">No files travel with this graph.</p>
            ) : (
              <>
                <p className="save-for-review-tip" id="save-for-review-file-list-heading">
                  These files will be copied for anyone who imports the graph:
                </p>
                <ul className="save-for-review-file-list" aria-labelledby="save-for-review-file-list-heading">
                  {files.files.map((file) => (
                    <li key={file.filename}>
                      <span className="save-for-review-file-name">{file.filename}</span>
                      <span className="save-for-review-file-size">{formatBytes(file.size_bytes)}</span>
                      <span className="save-for-review-file-used-by">
                        Used by {file.used_by.join(", ")}
                      </span>
                    </li>
                  ))}
                </ul>
              </>
            )}
          </div>
        )}
      </div>

      <fieldset className="save-for-review-lifetime">
        <legend>Link expires after</legend>
        {LIFETIME_CHOICES.map((choice) => (
          <label key={choice.seconds} className="save-for-review-option-label">
            <Input
              type="radio"
              name="save-for-review-lifetime"
              value={choice.seconds}
              checked={lifetime === choice.seconds}
              onChange={() => setLifetime(choice.seconds)}
            />
            <span>{choice.label}</span>
          </label>
        ))}
      </fieldset>

      <div className="save-for-review-configure-actions">
        <Button
          type="button"
          variant="primary"
          disabled={createDisabled || loading}
          aria-disabled={createDisabled || loading || undefined}
          title={createDisabledTitle}
          onClick={() => onCreate(importChecked, lifetime)}
          data-testid="save-for-review-create"
        >
          Create link
        </Button>
      </div>
    </div>
  );
}

/** Convert a backend path-only share_url to an absolute URL on the current
 *  origin. The frontend handles this rather than the backend so the same
 *  build can be deployed under any origin. */
function _toAbsoluteShareUrl(pathOnly: string): string {
  if (pathOnly.startsWith("http://") || pathOnly.startsWith("https://")) {
    return pathOnly;
  }
  return `${window.location.origin}${pathOnly}`;
}

export function SaveForReviewDialog(): JSX.Element | null {
  const dialogOpen = useShareableReviewStore((s) => s.dialogOpen);
  const latestResponse = useShareableReviewStore((s) => s.latestResponse);
  const inFlight = useShareableReviewStore((s) => s.inFlight);
  const error = useShareableReviewStore((s) => s.error);
  const close = useShareableReviewStore((s) => s.close);
  const request = useShareableReviewStore((s) => s.request);
  const openConfigure = useShareableReviewStore((s) => s.openConfigure);
  const confirmMark = useShareableReviewStore((s) => s.confirmMark);
  const validationResult = useExecutionStore((s) => s.validationResult);
  const completionReady =
    validationResult?.readiness?.completion_ready === true;

  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const copyTimeoutRef = useRef<number | null>(null);
  const dialogRef = useRef<HTMLElement>(null);

  // Real modal behaviour consistent with GraphModal / ConfirmDialog: trap Tab
  // focus inside the dialog, move initial focus to the always-present Close
  // button, and restore focus to the opener on unmount. `dialogOpen` gates the
  // trap; the component unmounts (returns null) when it closes, so the trap's
  // restore-on-cleanup runs.
  useFocusTrap(dialogRef, dialogOpen, ".save-for-review-close");

  useEffect(() => {
    return () => {
      if (copyTimeoutRef.current !== null) {
        window.clearTimeout(copyTimeoutRef.current);
      }
    };
  }, []);

  // WAI-ARIA Authoring Practices require role="dialog" + aria-modal="true"
  // modals to close on Escape. The listener is attached to `document` rather
  // than the dialog element — the canonical React modal pattern, matching
  // GraphModal / ConfirmDialog — so Escape closes regardless of which trapped
  // control currently holds focus. Effect re-runs when `dialogOpen` or `close`
  // change so the handler only listens while the dialog is open.
  useEffect(() => {
    if (!dialogOpen) return undefined;
    function _onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        close();
      }
    }
    document.addEventListener("keydown", _onKeyDown);
    return () => document.removeEventListener("keydown", _onKeyDown);
  }, [dialogOpen, close]);

  if (!dialogOpen) return null;

  const absoluteShareUrl = latestResponse !== null
    ? _toAbsoluteShareUrl(latestResponse.share_url)
    : null;

  async function _onCopy() {
    if (absoluteShareUrl === null) return;
    try {
      await navigator.clipboard.writeText(absoluteShareUrl);
      setCopyState("copied");
    } catch {
      // Clipboard API may be unavailable (HTTP / restrictive permissions).
      // The URL input is still selectable; user can copy manually.
      setCopyState("failed");
    }
    if (copyTimeoutRef.current !== null) {
      window.clearTimeout(copyTimeoutRef.current);
    }
    copyTimeoutRef.current = window.setTimeout(() => {
      setCopyState("idle");
    }, COPY_FEEDBACK_TIMEOUT_MS);
  }

  function _onRetry() {
    if (!completionReady) return;
    // Retry the mint with the options the owner chose. If the error phase is
    // gone (reset between attempts) this no-ops; the user closes and re-opens
    // via the CompletionBar.
    const current = useShareableReviewStore.getState().request;
    if (current.kind === "error") {
      void confirmMark(current.options);
    }
  }

  function _onReloadFiles() {
    const current = useShareableReviewStore.getState().request;
    if (current.kind === "configure") {
      void openConfigure(current.sessionId);
    }
  }

  return (
    <>
      <div
        className="save-for-review-dialog-backdrop"
        data-testid="save-for-review-dialog-backdrop"
        onClick={close}
        aria-hidden="true"
      />
      <section
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        className="save-for-review-dialog"
        aria-labelledby="save-for-review-dialog-title"
        data-testid="save-for-review-dialog"
      >
        <div className="save-for-review-dialog-content">
          <header>
            <h2 id="save-for-review-dialog-title">Share for review</h2>
          </header>

          {request.kind === "configure" && (
            <ConfigureStep
              files={request.files}
              filesError={request.filesError}
              createDisabled={!completionReady}
              createDisabledTitle={
                !completionReady
                  ? validationResult?.readiness?.blockers[0]?.detail ??
                    "Resolve completion blockers before creating a link."
                  : undefined
              }
              onCreate={(allowImport, lifetimeSeconds) =>
                void confirmMark({ allowImport, lifetimeSeconds })
              }
              onReloadFiles={_onReloadFiles}
            />
          )}

          {request.kind !== "configure" && inFlight && (
            <div role="status" data-testid="save-for-review-spinner">
              Minting share link...
            </div>
          )}

          {request.kind !== "configure" && !inFlight && error !== null && (
            <div role="alert" className="save-for-review-error" data-testid="save-for-review-error">
              <p>{error}</p>
              <Button
                compact
                type="button"
                onClick={_onRetry}
                disabled={!completionReady}
                aria-disabled={!completionReady || undefined}
                title={
                  !completionReady
                    ? validationResult?.readiness?.blockers[0]?.detail ??
                      "Resolve completion blockers before trying again."
                    : undefined
                }
                data-testid="save-for-review-retry"
              >
                Try again
              </Button>
            </div>
          )}

          {request.kind !== "configure" && !inFlight && error === null && latestResponse !== null && absoluteShareUrl !== null && (
            <div className="save-for-review-success" data-testid="save-for-review-success">
              <p>
                Share this link with your reviewer. It expires on{" "}
                <time dateTime={latestResponse.expires_at}>
                  {new Date(latestResponse.expires_at).toLocaleString()}
                </time>
                .
              </p>
              <div className="save-for-review-url-row">
                <label htmlFor="save-for-review-url">Share URL</label>
                <Input
                  id="save-for-review-url"
                  type="text"
                  readOnly
                  value={absoluteShareUrl}
                  onFocus={(e) => e.currentTarget.select()}
                  data-testid="save-for-review-url-input"
                />
                <Button
                  compact
                  type="button"
                  onClick={() => void _onCopy()}
                  data-testid="save-for-review-copy"
                  aria-label="Copy share URL to clipboard"
                >
                  {/* The failure message is NOT a third button label
                      (elspeth-6eb60a33a9). `.btn` and `.btn-compact` both
                      declare white-space: nowrap, so a full sentence made the
                      button grow, which reflowed this flex row under the
                      pointer that had just clicked it — the control moved away
                      at the moment the user was reacting to the error. The
                      label stays two states wide and the message renders
                      below the row, exactly as UserAdminDialog does it. */}
                  {copyState === "copied" ? "Copied!" : "Copy"}
                </Button>
                <a
                  href={absoluteShareUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  data-testid="save-for-review-open-link"
                >
                  Open in new tab
                </a>
              </div>
              {/* Live region for the copy outcome. It is mounted empty and only
                  its TEXT changes, because a region inserted with its content
                  already inside it is not reliably announced. role="status"
                  (polite) rather than role="alert": this dialog already owns an
                  assertive region for the mint failure above, and the user
                  triggered this message themselves by clicking Copy — it does
                  not warrant interrupting whatever is being read. The tone
                  class is applied with the text so an idle empty span paints
                  no banner. */}
              <span
                role="status"
                className={copyState === "failed" ? "save-for-review-error" : undefined}
                data-testid="save-for-review-copy-status"
              >
                {copyState === "failed"
                  ? "Copy failed — select the share URL above and copy it manually."
                  : ""}
              </span>
              <p className="save-for-review-tip">
                The link grants a different authenticated user read-only access
                to a frozen snapshot of this pipeline. Recipients must have an
                account on this deployment.
              </p>
            </div>
          )}

          <footer>
            <Button
              type="button"
              className="save-for-review-close"
              onClick={close}
              data-testid="save-for-review-close"
            >
              Close
            </Button>
          </footer>
        </div>
      </section>
    </>
  );
}
