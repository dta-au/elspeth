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
 * deliberately ABSENT — this is an inspect-only view, not a fork
 * surface (multi-user collaborative editing is out of scope per
 * plan 19a §"Scope boundaries").
 *
 * Error handling:
 *
 * * 401 (tampered/expired token) → "This share link is no longer valid"
 *   with a "Return to my workspace" CTA.
 * * 404 (blob reaped) → "This shared snapshot is no longer available"
 *   with a "Ask the sender for a fresh link" message.
 * * Other / network → "Couldn't load the shared pipeline" with retry.
 */

import { useEffect, useState, type CSSProperties } from "react";

import { fetchSharedInspect } from "@/api/shareableReviews";
import { ReadOnlyProvider } from "@/contexts/ReadOnlyContext";
import { SharedAuditReadinessPanel } from "./SharedAuditReadinessPanel";
import { YamlDisplay } from "@/components/inspector/YamlDisplay";
import { GraphMiniView } from "@/components/sidebar/GraphMiniView";
import type { ApiError, SharedInspectResponse } from "@/types/api";

interface SharedInspectViewProps {
  token: string;
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

function _classifyError(exc: unknown): { status: number | null; message: string } {
  if (_isApiError(exc)) {
    if (exc.status === 401) {
      return {
        status: 401,
        message:
          "This share link is no longer valid. The link may have expired or " +
          "been revoked. Ask the sender for a fresh link.",
      };
    }
    if (exc.status === 404) {
      return {
        status: 404,
        message:
          "This shared snapshot is no longer available. The original " +
          "operator may need to re-share — ask the sender for a fresh link.",
      };
    }
    return {
      status: exc.status,
      message: exc.detail ?? "Couldn't load the shared pipeline.",
    };
  }
  if (exc instanceof Error) {
    return { status: null, message: exc.message };
  }
  return { status: null, message: "Couldn't load the shared pipeline." };
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

  useEffect(() => {
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

  if (state.kind === "loading") {
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

  if (state.kind === "error") {
    return (
      <main
        role="main"
        className="shared-inspect-view shared-inspect-view--error"
        data-testid="shared-inspect-error"
        style={MAIN_STYLE}
      >
        <h1 style={H1_STYLE}>Shared link unavailable</h1>
        <p role="alert">{state.message}</p>
        <p>
          <a href={_returnToWorkspaceUrl()} data-testid="shared-inspect-return-link">
            Return to my workspace
          </a>
        </p>
      </main>
    );
  }

  const { response } = state;
  // pipeline_metadata is now typed `PipelineMetadata` with `name` /
  // `description` as `string | null`. Render an empty string when the
  // owner left the description blank; surface "Untitled pipeline" for a
  // null name.
  const pipelineName = response.pipeline_metadata.name ?? "Untitled pipeline";
  const pipelineDescription = response.pipeline_metadata.description ?? "";

  return (
    <ReadOnlyProvider value={true}>
      <main
        role="main"
        className="shared-inspect-view shared-inspect-view--loaded"
        data-testid="shared-inspect-loaded"
        style={MAIN_STYLE}
      >
        <header>
          <p className="shared-inspect-banner" role="status" style={BANNER_STYLE}>
            Read-only shared view. Shared by{" "}
            <strong>{response.created_by_user_id}</strong> on{" "}
            <time dateTime={response.created_at}>
              {new Date(response.created_at).toLocaleString()}
            </time>
            ; expires on{" "}
            <time dateTime={response.expires_at}>
              {new Date(response.expires_at).toLocaleString()}
            </time>
            .
          </p>
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
