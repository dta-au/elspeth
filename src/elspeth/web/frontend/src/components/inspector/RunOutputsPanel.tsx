// ============================================================================
// RunOutputsPanel
//
// Per-run audit-evidence inventory of every sink-write artefact, surfaced
// through the GET /api/runs/{rid}/outputs manifest endpoint. Each row
// renders the artefact's type, basename/URI, size, and short content
// hash, plus (for downloadable file artefacts):
//
//   * a Download anchor that hits /content
//   * an Expand-preview toggle that lazy-fetches /preview
//
// Distinct from the diagnostics-panel artifact list (capped at 20 for
// operator-UI pacing): this panel is the unbounded canonical view.
//
// Non-file artefacts (DatabaseSink, Dataverse webhook, Azure blob
// without filesystem mirror) are listed as metadata-only — honest
// evidence the run produced them, no fake action buttons.
// ============================================================================

import { useEffect, useRef, useState } from "react";
import {
  downloadRunOutputContent,
  fetchRunOutputPreview,
  fetchRunOutputs,
} from "@/api/client";
import {
  Button,
  PreviewTable,
  StructuredJsonPreview,
  type PreviewTableModel,
} from "@/components/ui";
import { parseCsvRows } from "@/utils/contentStructure";
import { absoluteTime } from "@/utils/time";
import { plural } from "@/utils/plural";
import type {
  ApiError,
  RunOutputArtifact,
  RunOutputArtifactPreview,
  RunOutputsResponse,
} from "@/types/index";

function isApiError(value: unknown): value is ApiError {
  return (
    typeof value === "object" &&
    value !== null &&
    "status" in value &&
    "detail" in value
  );
}

function formatError(value: unknown, fallback: string): string {
  if (isApiError(value)) {
    return value.detail || fallback;
  }
  if (value instanceof Error) {
    return value.message;
  }
  return fallback;
}

/**
 * Trigger a browser download from an in-memory Blob. Uses a
 * synthetic anchor + object URL because the `/content` endpoint
 * requires Authorization headers — a plain `<a href>` would 401 on
 * top-level navigation. Mirrors `blobStore.downloadBlob`.
 */
function triggerBrowserDownload(data: Blob, filename: string): void {
  const url = URL.createObjectURL(data);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.appendChild(anchor);
  anchor.click();
  document.body.removeChild(anchor);
  URL.revokeObjectURL(url);
}

interface RunOutputsPanelProps {
  runId: string;
}

interface PreviewState {
  status: "loading" | "loaded" | "error" | "purged";
  preview?: RunOutputArtifactPreview;
  error?: string;
}

const HASH_DISPLAY_LENGTH = 12;

/**
 * Gap between an artifact row and the preview block beneath it. Named once and
 * expressed as the token rather than the raw `6` it was repeated as at five
 * sites: --space-1-5 IS 6px, so the spacing now moves with the scale instead of
 * being invisible to it (elspeth-cda90fbb49).
 */
const PREVIEW_BLOCK_MARGIN_TOP = "var(--space-1-5)";

function formatBytes(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KiB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MiB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GiB`;
}

function basenameOf(pathOrUri: string): string {
  // Strip file:// prefix, then take the last segment.
  const stripped = pathOrUri.startsWith("file://") ? pathOrUri.slice(7) : pathOrUri;
  const idx = stripped.lastIndexOf("/");
  return idx === -1 ? stripped : stripped.slice(idx + 1);
}

/**
 * "blob" and "payload" are elspeth's own opaque internal storage — a
 * content hash or blob id as a filename, meaningless to an operator.
 * Server-classified (see storage_kind on the wire type) against the
 * REAL storage layouts, not guessed from path shape: replaces a former
 * frontend regex heuristic that matched a layout no repo code actually
 * produced (elspeth-52af16f9ae).
 */
function isInternalStoragePath(artifact: RunOutputArtifact): boolean {
  return (
    artifact.storage_kind === "blob" || artifact.storage_kind === "payload"
  );
}

function artifactDisplayName(artifact: RunOutputArtifact): string {
  if (!isFileArtifact(artifact)) {
    return artifact.path_or_uri;
  }
  if (isInternalStoragePath(artifact)) {
    return artifact.sink_node_id || "artifact";
  }
  return basenameOf(artifact.path_or_uri);
}

function artifactDisplayTitle(artifact: RunOutputArtifact): string {
  if (isFileArtifact(artifact) && isInternalStoragePath(artifact)) {
    return `Recorded artifact for ${artifact.sink_node_id || "artifact"}`;
  }
  if (isFileArtifact(artifact)) {
    return artifactDisplayName(artifact);
  }
  return artifact.path_or_uri;
}

function isFileArtifact(artifact: RunOutputArtifact): boolean {
  // The backend reports artifact_type="file" for filesystem outputs;
  // anything else (database, webhook) is non-file evidence.
  return artifact.artifact_type === "file";
}

export function RunOutputsPanel({ runId }: RunOutputsPanelProps) {
  const [manifest, setManifest] = useState<RunOutputsResponse | null>(null);
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [previewByArtifactId, setPreviewByArtifactId] = useState<
    Record<string, PreviewState>
  >({});
  // Tracks which artifact rows the operator has expanded for preview.
  const [expandedArtifactIds, setExpandedArtifactIds] = useState<Set<string>>(
    new Set(),
  );
  const activeRunIdRef = useRef(runId);
  const manifestRequestSeqRef = useRef(0);
  const previewRunGenerationRef = useRef(0);
  // Which run OWNS the manifest currently in state. On a runId prop change
  // A→B the auto-expand effect fires in the same commit with the NEW runId
  // but the render-time manifest still run A's — acting on that pair would
  // consume run B's auto-expand budget on run A's artifact ids (a cross-run
  // /preview fetch that 404s). The effect therefore only acts when the
  // manifest provably belongs to the current runId.
  const manifestRunIdRef = useRef<string | null>(null);
  // Runs that have already had their single previewable artifact
  // auto-expanded (elspeth-3a7b7c7b37). Once-per-run so a manual collapse or
  // a Refresh of the same run is never overridden — a SET, not a single id,
  // so an A→B→A run flip can never re-arm run A's auto-expand over the
  // operator's collapse.
  const autoExpandedRunIdsRef = useRef<Set<string>>(new Set());

  const loadManifest = async (
    targetRunId: string,
    options: { clearRunScopedState?: boolean } = {},
  ) => {
    const requestSeq = ++manifestRequestSeqRef.current;
    activeRunIdRef.current = targetRunId;
    if (options.clearRunScopedState) {
      previewRunGenerationRef.current += 1;
      manifestRunIdRef.current = null;
      setManifest(null);
      setPreviewByArtifactId({});
      setExpandedArtifactIds(new Set());
    }
    setIsLoading(true);
    setError(null);
    try {
      const response = await fetchRunOutputs(targetRunId);
      if (
        requestSeq !== manifestRequestSeqRef.current ||
        targetRunId !== activeRunIdRef.current
      ) {
        return;
      }
      manifestRunIdRef.current = targetRunId;
      setManifest(response);
    } catch (err) {
      if (
        requestSeq !== manifestRequestSeqRef.current ||
        targetRunId !== activeRunIdRef.current
      ) {
        return;
      }
      setError(formatError(err, "Failed to load outputs"));
    } finally {
      if (
        requestSeq === manifestRequestSeqRef.current &&
        targetRunId === activeRunIdRef.current
      ) {
        setIsLoading(false);
      }
    }
  };

  const handleDownload = async (artifact: RunOutputArtifact) => {
    try {
      const { data, filename } = await downloadRunOutputContent(
        runId,
        artifact.artifact_id,
      );
      triggerBrowserDownload(data, filename);
    } catch (err) {
      // Surface the failure inline against the manifest banner; less
      // intrusive than a toast, and the operator can click Refresh.
      setError(formatError(err, "Download failed"));
    }
  };

  useEffect(() => {
    void loadManifest(runId, { clearRunScopedState: true });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [runId]);

  // Auto-expand the single-previewable-artifact common case: previews were
  // opt-in only, so the routine one-output run landed collapsed
  // (elspeth-3a7b7c7b37). Routed through togglePreview so the lazy /preview
  // fetch keeps its request-sequencing guards (manifestRequestSeqRef /
  // previewRunGenerationRef) — never a parallel fetch path. Multi-artifact
  // manifests stay collapsed.
  useEffect(() => {
    if (
      manifest === null ||
      manifestRunIdRef.current !== runId ||
      autoExpandedRunIdsRef.current.has(runId)
    ) {
      return;
    }
    const previewable = manifest.artifacts.filter(
      (artifact) =>
        isFileArtifact(artifact) && artifact.exists_now && artifact.downloadable,
    );
    if (previewable.length !== 1) return;
    autoExpandedRunIdsRef.current.add(runId);
    void togglePreview(previewable[0]);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [manifest, runId]);

  const togglePreview = async (artifact: RunOutputArtifact) => {
    const next = new Set(expandedArtifactIds);
    if (next.has(artifact.artifact_id)) {
      next.delete(artifact.artifact_id);
      setExpandedArtifactIds(next);
      return;
    }
    next.add(artifact.artifact_id);
    setExpandedArtifactIds(next);

    // Skip refetch if already loaded.
    if (previewByArtifactId[artifact.artifact_id]?.status === "loaded") {
      return;
    }
    setPreviewByArtifactId((prev) => ({
      ...prev,
      [artifact.artifact_id]: { status: "loading" },
    }));
    const targetRunId = runId;
    const previewRunGeneration = previewRunGenerationRef.current;
    try {
      const preview = await fetchRunOutputPreview(targetRunId, artifact.artifact_id);
      if (
        previewRunGeneration !== previewRunGenerationRef.current ||
        targetRunId !== activeRunIdRef.current
      ) {
        return;
      }
      setPreviewByArtifactId((prev) => ({
        ...prev,
        [artifact.artifact_id]: { status: "loaded", preview },
      }));
    } catch (err) {
      if (
        previewRunGeneration !== previewRunGenerationRef.current ||
        targetRunId !== activeRunIdRef.current
      ) {
        return;
      }
      // The preview endpoint returns 410 + error_type=artifact_purged_or_moved
      // when the file existed at manifest time but is gone now (purge race).
      // We surface this as a per-row "no longer available on disk" state
      // rather than a generic toast. Match on the structured error_type
      // field rather than string-matching the human-readable detail —
      // detail wording can change without breaking the contract.
      const isPurgedRace =
        isApiError(err) && err.error_type === "artifact_purged_or_moved";
      setPreviewByArtifactId((prev) => ({
        ...prev,
        [artifact.artifact_id]: isPurgedRace
          ? { status: "purged" }
          : {
              status: "error",
              error: formatError(err, "Preview failed"),
            },
      }));
    }
  };

  return (
    <section
      aria-label="Run outputs"
      className="run-outputs-panel"
    >
      <div className="run-outputs-panel-header">
        <span className="run-outputs-panel-title">Outputs</span>
        <Button
          compact
          onClick={() => void loadManifest(runId)}
          disabled={isLoading}
        >
          {isLoading ? "Loading…" : "Refresh"}
        </Button>
      </div>

      {error && (
        <div role="alert" className="run-outputs-panel-error">
          {error}
        </div>
      )}

      {!manifest && !error && isLoading && (
        <div className="run-outputs-panel-muted">
          {/* The shared .spinner, same as LoginPage / AuthGuard / ExecuteButton
              / PluginCard: static text cannot tell an operator whether the
              panel is working or stalled (elspeth-cda90fbb49). */}
          <span className="spinner" aria-hidden="true" /> Loading outputs…
        </div>
      )}

      {manifest && manifest.artifacts.length === 0 && !isLoading && (
        <div className="run-outputs-panel-muted">
          This run produced no outputs.
        </div>
      )}

      {manifest && manifest.artifacts.length > 0 && (
        <ul className="run-output-artifact-list">
          {manifest.artifacts.map((artifact) => {
            const expanded = expandedArtifactIds.has(artifact.artifact_id);
            const previewState = previewByArtifactId[artifact.artifact_id];
            const isFile = isFileArtifact(artifact);
            return (
              <li
                key={artifact.artifact_id}
                className="run-output-artifact-item"
              >
                <ArtifactRow
                  artifact={artifact}
                  expanded={expanded}
                  onTogglePreview={() => void togglePreview(artifact)}
                  onDownload={() => void handleDownload(artifact)}
                />
                {isFile && expanded && (
                  <ArtifactPreviewView
                    previewState={previewState}
                    onDownload={() => void handleDownload(artifact)}
                  />
                )}
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

// ── ArtifactRow ─────────────────────────────────────────────────────────────

interface ArtifactRowProps {
  artifact: RunOutputArtifact;
  expanded: boolean;
  onTogglePreview: () => void;
  onDownload: () => void;
}

function ArtifactRow({ artifact, expanded, onTogglePreview, onDownload }: ArtifactRowProps) {
  const isFile = isFileArtifact(artifact);
  const displayName = artifactDisplayName(artifact);
  const shortHash = artifact.content_hash.slice(0, HASH_DISPLAY_LENGTH);
  const displayTitle = artifactDisplayTitle(artifact);

  return (
    <div className="run-output-artifact-row">
      <span className="run-output-artifact-kind">
        {artifact.artifact_type}
      </span>
      <span
        className="run-output-artifact-name"
        title={displayTitle}
      >
        {displayName}
      </span>
      <span className="run-output-artifact-meta">
        {formatBytes(artifact.size_bytes)}
      </span>
      {/* Run timestamp — same class family as file size so the two pieces
          of inline per-artifact metadata read as siblings. Tooltip carries the
          unmodified wire-format timestamp (with timezone marker) for
          anyone diffing against the audit DB directly. */}
      <span
        className="run-output-artifact-meta run-output-artifact-time"
        title={artifact.created_at}
      >
        {absoluteTime(artifact.created_at)}
      </span>
      <span
        className="run-output-artifact-hash"
        title={`SHA-256 ${artifact.content_hash}`}
      >
        {shortHash}…
      </span>
      {isFile && (
        <ArtifactActions
          artifact={artifact}
          expanded={expanded}
          onTogglePreview={onTogglePreview}
          onDownload={onDownload}
        />
      )}
    </div>
  );
}

interface ArtifactActionsProps {
  artifact: RunOutputArtifact;
  expanded: boolean;
  onTogglePreview: () => void;
  onDownload: () => void;
}

function ArtifactActions({ artifact, expanded, onTogglePreview, onDownload }: ArtifactActionsProps) {
  if (!artifact.exists_now) {
    return (
      <span
        className="run-output-artifact-unavailable"
        title={artifactDisplayTitle(artifact)}
      >
        no longer available on disk
      </span>
    );
  }
  if (!artifact.downloadable) {
    // File exists on disk but is outside the sink-allowlist that the
    // /content endpoint enforces. The audit row is honest evidence;
    // the download is refused for defence-in-depth.
    return (
      <span
        className="run-output-artifact-unavailable"
        title="The recorded path is outside the sink output allowlist; the server refuses to serve its bytes."
      >
        outside allowed sink directories
      </span>
    );
  }
  return (
    <span className="run-output-artifact-actions">
      <Button
        compact
        onClick={onTogglePreview}
        aria-expanded={expanded}
      >
        {expanded ? "Hide preview" : "Preview"}
      </Button>
      <Button
        compact
        onClick={onDownload}
      >
        Download
      </Button>
    </span>
  );
}

// ── ArtifactPreviewView ────────────────────────────────────────────────────

interface ArtifactPreviewViewProps {
  previewState?: PreviewState;
  onDownload: () => void;
}

function ArtifactPreviewView({
  previewState,
  onDownload,
}: ArtifactPreviewViewProps) {
  if (!previewState || previewState.status === "loading") {
    return (
      <div style={{ marginTop: PREVIEW_BLOCK_MARGIN_TOP, color: "var(--color-text-muted)" }}>
        {/* The shared .spinner, same as LoginPage / AuthGuard / ExecuteButton /
            PluginCard. This is the only genuinely IN-FLIGHT state in this
            view — purged / error / binary below are terminal outcomes, and a
            spinner on one of those would claim work that is not happening
            (elspeth-cda90fbb49). */}
        <span className="spinner" aria-hidden="true" /> Loading preview…
      </div>
    );
  }
  if (previewState.status === "purged") {
    return (
      <div style={{ marginTop: PREVIEW_BLOCK_MARGIN_TOP, color: "var(--color-text-muted)", fontStyle: "italic" }}>
        File is no longer available on disk (purged or moved between manifest fetch and preview).
      </div>
    );
  }
  if (previewState.status === "error") {
    return (
      <div role="alert" style={{ marginTop: PREVIEW_BLOCK_MARGIN_TOP, color: "var(--color-error)" }}>
        {previewState.error ?? "Preview failed"}
      </div>
    );
  }
  const preview = previewState.preview;
  if (!preview) return null;
  if (preview.content_type === "binary") {
    return (
      <div style={{ marginTop: PREVIEW_BLOCK_MARGIN_TOP, color: "var(--color-text-muted)", fontStyle: "italic" }}>
        Binary file — no inline preview available. Use the Download button to inspect.
      </div>
    );
  }
  return (
    <div style={{ marginTop: PREVIEW_BLOCK_MARGIN_TOP }}>
      {preview.content_type === "json" ? (
        <StructuredJsonPreview
          text={preview.preview_text}
          truncated={preview.truncated}
        />
      ) : preview.content_type === "csv" || preview.content_type === "jsonl" ? (
        <TabularPreview
          text={preview.preview_text}
          contentType={preview.content_type}
          truncated={preview.truncated}
        />
      ) : (
        <pre
          style={{
            maxHeight: 300,
            overflow: "auto",
            backgroundColor: "var(--color-surface-hover)",
            padding: 8,
            borderRadius: "var(--radius-sm)",
            margin: 0,
            fontSize: 11,
            whiteSpace: "pre-wrap",
            overflowWrap: "anywhere",
          }}
        >
          {preview.preview_text}
        </pre>
      )}
      {preview.truncated && (
        <div
          style={{
            marginTop: "var(--space-xs)",
            color: "var(--color-text-muted)",
            fontSize: 11,
          }}
        >
          {/* row_count_preview is a PHYSICAL LINE count: the backend caps
              with `splitlines()` (web/execution/preview.py:162), so a CSV
              whose values contain newlines yields far fewer records than
              lines. Calling them rows asserted a number the table below
              visibly contradicts. */}
          Preview truncated
          {preview.row_count_preview != null &&
            ` to ${plural(preview.row_count_preview, "line")}`}
          {" — "}
          <Button variant="bare" className="link-button" onClick={onDownload}>
            download for full file
          </Button>
          {" "}({formatBytes(preview.total_size_bytes)} total).
        </div>
      )}
    </div>
  );
}

// ── TabularPreview ─────────────────────────────────────────────────────────

/**
 * A rendered table plus the reason a record is missing from it, if one is.
 *
 * Local, not a widening of the shared `PreviewTableModel`: the caveat is a
 * property of reading CSV/JSONL text, and StructuredJsonPreview — the other
 * consumer of that shared type — has nothing to say through it.
 */
interface TabularPreviewModel extends PreviewTableModel {
  caveat: string | null;
}

interface TabularPreviewProps {
  text: string;
  contentType: "csv" | "jsonl";
  /** Whether the backend cut the preview short. Governs how an unterminated
   *  final field is explained — see buildTabularPreviewModel. */
  truncated: boolean;
}

/**
 * Builds a headers+rows model for the shared PreviewTable out of raw
 * csv/jsonl preview text.
 *
 * Two content types feed this:
 *   * csv  — parsed with the shared `parseCsvRows` reader
 *            (`utils/contentStructure.ts`), which honours RFC4180 quoting:
 *            a delimiter or a newline INSIDE a quoted field is data, and
 *            `""` is one literal quote. This used to be a bare
 *            `line.split(delimiter)`, which shredded every quoted value
 *            containing a comma — universal in practice, because every
 *            transform:llm emits `<response_field>_usage` as a dict repr
 *            full of them. Body rows then parsed wider than the header, so
 *            `columnCount` grew to the body width and the header was
 *            right-padded with EMPTY strings: real values rendered under
 *            nameless columns with no signal anything was wrong
 *            (elspeth-7f1e148ed6).
 *
 *            The backend tags both `.csv` and `.tsv` files as content_type
 *            "csv" (see web/execution/preview._CSV_EXTENSIONS), so we still
 *            sniff the first physical line for tab vs comma rather than
 *            hardcoding `,`. Without this, TSV rows collapse into a single
 *            column. The first parsed row is the real header row.
 *
 *            Quote handling stays ON for the tab delimiter, deliberately:
 *            the sink writes through `csv.DictWriter(delimiter=...)`
 *            (plugins/sinks/csv_sink.py), which quotes a field containing
 *            the delimiter, a quote, or a newline whatever the delimiter
 *            is. Reading TSV with quoting disabled would therefore
 *            misparse ELSPETH's own tab-separated output.
 *   * jsonl — each line is a JSON object that must NOT be split on
 *             commas (that fragments the JSON across cells). Each line
 *             is rendered as a single-column row under one synthetic
 *             "value" header — jsonl has no column structure of its own,
 *             but every PreviewTable still gets a real th scope="col"
 *             header cell rather than the old bold-td fake header
 *             (elspeth-611a05668e).
 *
 * Still deliberately tolerant of ragged rows: short rows are padded and
 * over-wide rows widen the header, because a preview should render what is
 * there rather than refuse. What it no longer does is MANUFACTURE that
 * raggedness out of correctly-quoted input.
 */
function buildTabularPreviewModel(
  text: string,
  contentType: "csv" | "jsonl",
  truncated: boolean,
): TabularPreviewModel | null {
  if (contentType === "jsonl") {
    const lines = text.split("\n").filter((line) => line.length > 0);
    if (lines.length === 0) {
      return null;
    }
    return {
      headers: ["value"],
      rows: lines.map((line) => [line]),
      caveat: null,
    };
  }

  // Sniff on the first physical line: a header row is never itself quoted
  // across a newline, so this is safe to do before parsing.
  const firstLine = text.split("\n", 1)[0] ?? "";
  const tabCount = (firstLine.match(/\t/g) ?? []).length;
  const commaCount = (firstLine.match(/,/g) ?? []).length;
  const delimiter = tabCount > commaCount ? "\t" : ",";

  const { rows: parsed, endedInQuotes } = parseCsvRows(text, delimiter);
  // An unterminated quoted field means the text stopped mid-record. That
  // trailing row is a fragment, not a record, so rendering it would show the
  // operator data that does not exist — drop it. But dropping it SILENTLY is
  // the defect this once had: the reason the record vanished has to be on
  // screen, and the two reasons are not the same.
  //
  // `truncated` is what distinguishes them, which is why it is threaded in
  // here rather than assumed. This function used to justify the silent drop
  // by pointing at the "Preview truncated" notice — a notice it could not
  // see, gated on a flag it was never given, and absent entirely on the
  // malformed-artifact path. summarizeCsv (utils/contentStructure.ts) reads
  // the SAME endedInQuotes flag and has always reported the malformed case
  // as a caveat; this is that rule, applied to the second consumer.
  const rows_ = endedInQuotes ? parsed.slice(0, -1) : parsed;
  const caveat = endedInQuotes
    ? truncated
      // Real and common: the backend's physical-line cap routinely lands
      // mid-value on LLM output, whose prose responses span many lines.
      ? "The last record was cut off by the preview limit and is not shown — download for the full file."
      : "Content has an unterminated quoted field — the final record could not be read."
    : null;
  if (rows_.length === 0) {
    return null;
  }

  const [headerRow = [], ...bodyRows] = rows_;
  const columnCount =
    bodyRows.length === 0
      ? headerRow.length
      : Math.max(headerRow.length, ...bodyRows.map((row) => row.length));
  const headers = Array.from({ length: columnCount }, (_, i) => headerRow[i] ?? "");
  const rows = bodyRows.map((row) =>
    Array.from({ length: columnCount }, (_, i) => row[i] ?? ""),
  );
  return { headers, rows, caveat };
}

function TabularPreview({ text, contentType, truncated }: TabularPreviewProps) {
  const table = buildTabularPreviewModel(text, contentType, truncated);
  if (!table) {
    return null;
  }
  return (
    <>
      <PreviewTable table={table} />
      {table.caveat !== null && (
        <div
          style={{
            marginTop: "var(--space-xs)",
            color: "var(--color-text-muted)",
            fontSize: 11,
          }}
        >
          {table.caveat}
        </div>
      )}
    </>
  );
}
