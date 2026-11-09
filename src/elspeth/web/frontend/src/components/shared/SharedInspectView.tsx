/**
 * SharedInspectView — Phase 6B Task 8 (refactored under FIX-C).
 *
 * Read-only inspect view rendered when the URL hash is in the form
 * `#/shared/{token}`. Mounted by App.tsx as a top-level branch that
 * short-circuits the regular composer UI.
 *
 * The token is a CAPABILITY, not an authenticator — the reviewer must
 * still be logged in. AuthGuard wraps this view so unauthenticated
 * users hit the login flow; on successful auth they land back here
 * via the persisted hash (the AuthGuard preserves the hash through
 * the login redirect via sessionStorage).
 *
 * FIX-C refactor (2026-05-19):
 *
 *   The initial Phase 6B Task 8 implementation rendered the readiness
 *   panel as an inline 3-column `<table>` and the YAML as a raw
 *   `<pre><code>` block, both inline in this file. The spec-compliance
 *   reviewer flagged that as NON-COMPLIANT vs. the plan's "shared
 *   inspect mounts a read-only AuditReadinessPanel + reused YamlView"
 *   description. The plan's literal references were aspirational —
 *   `InspectorPanel` doesn't exist in this codebase, and YamlView /
 *   GraphMiniView are session-store-coupled — so the FIX-C remediation
 *   extracted reusable primitives:
 *
 *     - `AuditReadinessRow` (extracted from AuditReadinessPanel)
 *     - `SharedAuditReadinessPanel` (renders 6 rows from a frozen
 *       snapshot, no live overlays, wrapped in ReadOnlyProvider)
 *     - `YamlDisplay` (extracted from YamlView; pure renderer, no
 *       store coupling)
 *     - `GraphMiniView.compositionStateOverride` prop
 *     - `ReadOnlyContext` / `useReadOnly`
 *
 *   The view is wrapped in `<ReadOnlyProvider value={true}>` so any
 *   descendant that respects the read-only signal disables its action
 *   affordances automatically.
 *
 * The view renders:
 *
 *   * Pipeline metadata (name, description).
 *   * `<SharedAuditReadinessPanel>` for the frozen six-row panel.
 *   * `<GraphMiniView compositionStateOverride={...} />` for the
 *     structural view (the GraphModal that the click would dispatch
 *     is NOT mounted in this surface — see GraphMiniView's prop
 *     docstring).
 *   * `<YamlDisplay>` for the frozen YAML.
 *
 * The composer chat panel, run controls, and edit affordances are
 * deliberately ABSENT — the view itself is read-only. When the link allows
 * it (`import_allowed`), the one write affordance is "Import a copy into my
 * workspace": it forks the frozen snapshot (and its files) into a NEW session
 * owned by the viewer and navigates there. The shared snapshot is never
 * modified; multi-user collaborative editing remains out of scope per plan 19a
 * §"Scope boundaries". The import control and its file list render outside
 * any `useReadOnly` consumer, so the read-only provider does not disable it.
 *
 * Error handling:
 *
 * * Authentication 401 → sign-in recovery, without declaring the link invalid.
 * * Capability 401 (tampered/expired token) → "This share link is no longer valid"
 *   with a "Return to my workspace" CTA.
 * * 404 (blob reaped) → "This shared snapshot is no longer available"
 *   with a "Ask the sender for a fresh link" message.
 * * Other / network → "Couldn't load the shared pipeline" with retry.
 *
 * Import errors keep the loaded view on screen and show an inline alert:
 * capability 401 → expired, 403 → not allowed, 404 → ask for a fresh link,
 * 413 → storage quota, authentication 401 → sign-in recovery, anything else →
 * generic message with the button re-enabled for retry.
 */

import { type JSX, useEffect, useRef, useState, type CSSProperties } from "react";

import { Button } from "@/components/ui";

import { fetchSharedInspect, importSharedSnapshot } from "@/api/shareableReviews";
import { useSessionStore } from "@/stores/sessionStore";
import { formatBytes } from "@/utils/bytes";
import { ReadOnlyProvider } from "@/contexts/ReadOnlyContext";
import { SharedAuditReadinessPanel } from "./SharedAuditReadinessPanel";
import { YamlDisplay } from "@/components/inspector/YamlDisplay";
import { GraphMiniView } from "@/components/sidebar/GraphMiniView";
import type { ApiError, SharedInspectResponse } from "@/types/api";

interface SharedInspectViewProps {
  token: string | null;
}

/**
 * Scroll ownership for the shared-inspect page (elspeth-2ff1b0b4ad).
 *
 * `.shared-inspect-view` is declared by NO stylesheet, so this `<main>` was an
 * unstyled flex item inside `.app-root` (`display: flex; flex-direction:
 * column; height: 100dvh`, header.css:12-17) under `body { overflow: hidden }`
 * (base.css:14-19). NO element on the page claimed the scroll, so the
 * readiness rows, the YAML and the "Return to my workspace" link were cut at
 * the fold with no scrollbar and no wheel response — on the one artifact a
 * user hands to a reviewer.
 *
 * `min-height: 0` is the load-bearing half: a column flex item defaults to
 * `min-height: auto`, which floors it at its content height and defeats
 * `overflow-y: auto` outright. With the floor released the item's height is
 * the (definite) 100dvh line, so the overflow is real and scrolls here.
 *
 * These are inline styles rather than the class the ticket asks for because
 * defining `.shared-inspect-view` needs a new stylesheet registered in
 * `styles/index.css`, which this lane does not own. The class names are left
 * on the elements so the CSS extraction is a lift-and-shift.
 */
const MAIN_STYLE: CSSProperties = {
  flex: 1,
  minHeight: 0,
  overflowY: "auto",
  width: "100%",
  // Measure cap on the house pattern for a full-bleed frame
  // (.recovery-panel, recovery.css:21-22) — otherwise the YAML and the
  // readiness rows run the full 1920px.
  maxWidth: "1080px",
  marginInline: "auto",
  padding: "var(--space-xl)",
};

/** Product type scale for the two bare headings (browser default is 32/24px). */
const H1_STYLE: CSSProperties = {
  margin: "0 0 var(--space-md)",
  fontSize: "var(--font-size-xl)",
};
const H2_STYLE: CSSProperties = {
  margin: "0 0 var(--space-sm)",
  fontSize: "var(--font-size-lg)",
};

/**
 * Read-only provenance banner ("Shared by X … expires on DATE"). Inline for
 * the same lift-and-shift reason as MAIN_STYLE above — it previously had
 * neither a rule nor an inline style, so the page's one provenance
 * disclosure rendered as ordinary paragraph prose (elspeth-729872658a).
 * De-emphasised secondary register: it must read as page chrome, not as
 * part of the shared pipeline's content.
 */
const BANNER_STYLE: CSSProperties = {
  margin: "0 0 var(--space-md)",
  color: "var(--color-text-secondary)",
  fontSize: "var(--font-size-sm)",
};

const FILE_LIST_STYLE: CSSProperties = {
  margin: "0 0 var(--space-md)",
  paddingLeft: "var(--space-lg)",
  color: "var(--color-text-secondary)",
  fontSize: "var(--font-size-sm)",
};

const NOT_FOUND_MESSAGE =
  "This shared snapshot is no longer available. The original " +
  "operator may need to re-share — ask the sender for a fresh link.";

type LoadState =
  | { kind: "loading" }
  | { kind: "loaded"; response: SharedInspectResponse }
  | { kind: "error"; status: number | null; message: string };

function _isApiError(value: unknown): value is ApiError {
  return (
    typeof value === "object" &&
    value !== null &&
    typeof (value as { status?: unknown }).status === "number"
  );
}

interface ClassifiedError {
  status: number | null;
  message: string;
  /** A bare-bearer 401: the viewer's sign-in, not the link, was refused. */
  authentication: boolean;
}

function _classifyError(exc: unknown): ClassifiedError {
  if (_isApiError(exc)) {
    if (exc.status === 401) {
      if (exc.error_type === "authentication_required") {
        return {
          status: 401,
          authentication: true,
          message: "Your sign-in was not accepted for this request. Reload this link or sign in again.",
        };
      }
      return {
        status: 401,
        authentication: false,
        message:
          "This share link is no longer valid. The link may have expired or " +
          "been revoked. Ask the sender for a fresh link.",
      };
    }
    if (exc.status === 404) {
      return {
        status: 404,
        authentication: false,
        message: NOT_FOUND_MESSAGE,
      };
    }
    return {
      status: exc.status,
      authentication: false,
      message: exc.detail ?? "Couldn't load the shared pipeline.",
    };
  }
  if (exc instanceof Error) {
    return { status: null, authentication: false, message: exc.message };
  }
  return { status: null, authentication: false, message: "Couldn't load the shared pipeline." };
}

/** User-facing copy for a failed import, on top of the shared classifier. */
function _importFailureMessage(exc: unknown): string {
  const classified = _classifyError(exc);
  if (classified.authentication) return classified.message;
  switch (classified.status) {
    case 401:
      return "This share link has expired";
    case 403:
      return "This link doesn't allow importing";
    case 404:
      return classified.message;
    case 413:
      return "You don't have enough storage to import these files";
    default:
      return "Couldn't import the shared pipeline";
  }
}

function _returnToWorkspaceUrl(): string {
  return `${window.location.origin}${window.location.pathname}`;
}

// FIX-K (2026-05-19): the previous `_narrowCompositionSnapshot` helper
// validated `composition_snapshot.nodes` / `.outputs` at the renderer.
// That validation now lives at the wire boundary in
// `validateSharedInspectResponse` (api/shareableReviews.ts) — per-field
// per-shape, with `composition_snapshot` typed as `CompositionState`
// directly (plan 19b:100-101). The renderer is no longer the
// trust-boundary check, so the narrow helper was deleted as redundant.

export function SharedInspectView({ token }: SharedInspectViewProps): JSX.Element {
  const [state, setState] = useState<LoadState>({ kind: "loading" });
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<{ status: number | null; message: string } | null>(null);
  const importingRef = useRef(false);

  useEffect(() => {
    if (token === null) return;
    let cancelled = false;
    const controller = new AbortController();
    setState({ kind: "loading" });
    fetchSharedInspect(token, controller.signal)
      .then((response) => {
        if (cancelled) return;
        setState({ kind: "loaded", response });
      })
      .catch((exc: unknown) => {
        if (cancelled) return;
        const { status, message } = _classifyError(exc);
        setState({ kind: "error", status, message });
      });
    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [token]);

  async function importCopy(): Promise<void> {
    if (token === null || importingRef.current) return;
    importingRef.current = true;
    setImporting(true);
    setImportError(null);
    try {
      const imported = await importSharedSnapshot(token, new AbortController().signal);
      // The shared route is a separate App branch whose hash the session
      // router ignores: refresh the session list first so the composer tree
      // finds the new session, then switch to its canonical hash.
      await useSessionStore.getState().loadSessions();
      window.location.hash = `#/${imported.session_id}`;
    } catch (exc: unknown) {
      setImportError({ status: _classifyError(exc).status, message: _importFailureMessage(exc) });
      setImporting(false);
    } finally {
      importingRef.current = false;
    }
  }

  const displayState: LoadState = token === null
    ? {
        kind: "error",
        status: null,
        message: "This share link is malformed. Ask the sender for a fresh Share for review link.",
      }
    : state;

  if (displayState.kind === "loading") {
    return (
      <main
        role="main"
        className="shared-inspect-view"
        data-testid="shared-inspect-loading"
        aria-busy="true"
        style={MAIN_STYLE}
      >
        <p>Loading shared pipeline…</p>
      </main>
    );
  }

  if (displayState.kind === "error") {
    return (
      <main
        role="main"
        className="shared-inspect-view shared-inspect-view--error"
        data-testid="shared-inspect-error"
        style={MAIN_STYLE}
      >
        <h1 style={H1_STYLE}>Shared link unavailable</h1>
        <p role="alert">{displayState.message}</p>
        <p>
          <a href={_returnToWorkspaceUrl()} data-testid="shared-inspect-return-link">
            Return to my workspace
          </a>
        </p>
      </main>
    );
  }

  const { response } = displayState;
  // pipeline_metadata is now typed `PipelineMetadata` with `name` /
  // `description` as `string | null`. Render an empty string when the
  // owner left the description blank; surface "Untitled pipeline" for a
  // null name.
  const pipelineName = response.pipeline_metadata.name ?? "Untitled pipeline";
  const pipelineDescription = response.pipeline_metadata.description ?? "";
  // A share-link recipient has no login and cannot resolve the opaque
  // identity id, so show the username frozen into the signed snapshot.
  const sharedBy = response.created_by_username;

  return (
    <ReadOnlyProvider value={true}>
      <main
        role="main"
        className="shared-inspect-view shared-inspect-view--loaded"
        data-testid="shared-inspect-loaded"
        style={MAIN_STYLE}
      >
        <header>
          <p
            className="shared-inspect-banner"
            role="status"
            data-testid="shared-inspect-banner"
            style={BANNER_STYLE}
          >
            Read-only shared view. Shared by{" "}
            <strong data-testid="shared-inspect-shared-by">{sharedBy}</strong> on{" "}
            <time dateTime={response.created_at}>
              {new Date(response.created_at).toLocaleString()}
            </time>
            ; expires on{" "}
            <time dateTime={response.expires_at}>
              {new Date(response.expires_at).toLocaleString()}
            </time>
            .
          </p>
          {response.import_allowed && (
            <section
              aria-label="Import a copy"
              data-testid="shared-inspect-import"
            >
              {response.files.length > 0 && (
                <ul
                  aria-label="Files that will be copied"
                  data-testid="shared-inspect-import-files"
                  style={FILE_LIST_STYLE}
                >
                  {response.files.map((file) => (
                    <li key={file.filename}>
                      {file.filename} ({formatBytes(file.size_bytes)})
                    </li>
                  ))}
                </ul>
              )}
              <Button
                type="button"
                variant="primary"
                disabled={importing || importError?.status === 403}
                onClick={() => void importCopy()}
              >
                Import a copy into my workspace
              </Button>
              {importError !== null && (
                <p role="alert" data-testid="shared-inspect-import-error">
                  {importError.message}
                </p>
              )}
            </section>
          )}
          <h1 style={H1_STYLE} data-testid="shared-inspect-pipeline-name">
            {pipelineName}
          </h1>
          {pipelineDescription !== "" && (
            <p data-testid="shared-inspect-pipeline-description">
              {pipelineDescription}
            </p>
          )}
        </header>

        <section
          aria-label="Pipeline structure (read-only)"
          data-testid="shared-inspect-graph"
        >
          <h2 style={H2_STYLE}>Pipeline structure</h2>
          <GraphMiniView compositionStateOverride={response.composition_snapshot} />
        </section>

        <section
          aria-label="Audit readiness panel (read-only)"
          data-testid="shared-inspect-audit-readiness"
        >
          <p>
            The owner reviewed this readiness panel at the moment of marking
            the pipeline ready for review. The values below are frozen — they
            reflect the owner's mark-time view, not the live state.
          </p>
          <SharedAuditReadinessPanel snapshot={response.audit_readiness} />
        </section>

        <section
          aria-label="Pipeline YAML (read-only)"
          data-testid="shared-inspect-yaml"
        >
          <h2 style={H2_STYLE}>Pipeline YAML</h2>
          <YamlDisplay yaml={response.yaml} filename="pipeline.yaml" />
        </section>

        <footer>
          <p>
            <a
              href={_returnToWorkspaceUrl()}
              data-testid="shared-inspect-return-link"
            >
              Return to my workspace
            </a>
          </p>
        </footer>
      </main>
    </ReadOnlyProvider>
  );
}
