/**
 * AuditReadinessRow — single row of the audit-readiness panel.
 *
 * Extracted from AuditReadinessPanel (Phase 6B FIX-C, plan Step 0)
 * so the same row primitive can render inside both the live composer
 * panel and the read-only `SharedAuditReadinessPanel`. The panel
 * passes a fully-prepared `RowPresentation` so the row component
 * stays a pure renderer — all the LLM-interpretations / inline-blob /
 * provenance overrides remain in the parent (where they belong: they
 * depend on multiple stores).
 *
 * Read-only signalling: the component consumes `useReadOnly()` and
 * forces the static (non-actionable) variant when true, regardless of
 * what `onSelect` the caller supplied. This is the load-bearing
 * contract that wraps a `SharedAuditReadinessPanel` subtree under
 * `<ReadOnlyProvider value={true}>` and gets actionability disabled
 * everywhere downstream. The live AuditReadinessPanel never wraps in
 * a provider (the default `false` value flows through) so its
 * actionable buttons render as today.
 *
 * Gate legibility (elspeth-088bf83922 T-2, option (a)): every row also
 * renders a small "Blocks run" / "Advisory" text badge next to its
 * heading. Parent panels prepare `blocksRun` from the same backend-readiness
 * helper used by ExecuteButton, so this pure renderer never reconstructs
 * admission from row status or summary text.
 *
 * Badge case (elspeth-1fbb371ac3): both values of this binary field are
 * SENTENCE case, so the pair reads as two states of one field rather than
 * two vocabularies. That is also the house register for badge text — every
 * other badge in the tree ships its label lowercase or sentence-cased
 * (`row union` in GraphView, `chat input` in InlineChatSourceEntry,
 * `completed with failures` in StatusBadge) and lets CSS supply any
 * upper-case badge register (shared.css `.status-badge`). Do not restore
 * "Blocks Run": the panel headers quote this literal in prose
 * (AuditReadinessPanel / SharedAuditReadinessPanel), so the label has to
 * read as an ordinary phrase inside a sentence.
 */

import { Button } from "@/components/ui";
import { useReadOnly } from "../../contexts/ReadOnlyContext";
import type { ReadinessRowId, ReadinessStatus } from "../../types/api";

/**
 * Prepared presentation values for a single row. Callers pre-compute
 * any glyph / aria / summary overrides (e.g. the llm_interpretations
 * stylised formatter, the inline-blob provenance override) and pass
 * the final values in. This keeps the row a pure renderer.
 */
export interface RowPresentation {
  id: ReadinessRowId;
  status: ReadinessStatus;
  /** Pre-resolved heading text (label or rowHeading() fallback). */
  heading: string;
  /** Pre-resolved summary text (after any overrides). */
  summaryText: string;
  /** Pre-resolved glyph character. */
  glyph: string;
  /** Accessible status label, read by SRs before the heading. */
  ariaStatusLabel: string;
  /** Backend-owned execution admission projected by the parent panel. */
  blocksRun: boolean;
  /**
   * Optional extra CSS modifier appended to the row's class list (e.g.
   * "audit-readiness-row--llm-interpretations"). Optional — most rows
   * don't need one.
   */
  extraClassName?: string;
  /**
   * Optional test id for the wrapping <li>. The live panel sets this
   * on the llm-interpretations row; other rows omit it.
   */
  testId?: string;
}

export interface AuditReadinessRowProps {
  /** The presentation values to render. */
  row: RowPresentation;
  /**
   * Click handler for actionable rows. When omitted OR when
   * `useReadOnly()` returns true, the row renders in its static
   * (non-clickable) variant. This is the read-only contract.
   */
  onSelect?: (rowId: ReadinessRowId) => void;
}

export function AuditReadinessRow({
  row,
  onSelect,
}: AuditReadinessRowProps): JSX.Element {
  const readOnly = useReadOnly();
  // Clickability requires (a) the row's status is actionable, (b) a
  // handler was provided, AND (c) the read-only signal is false. In
  // read-only mode every row renders static regardless of status —
  // shared inspect surfaces have no detail-drawer affordance.
  const isActionable = row.status === "warning" || row.status === "error";
  const clickable = isActionable && onSelect !== undefined && !readOnly;
  const baseClassName = `audit-readiness-row audit-readiness-row--${row.status}`;
  const className = row.extraClassName
    ? `${baseClassName} ${row.extraClassName}`
    : baseClassName;

  // Gate legibility (elspeth-088bf83922 T-2): render the parent-prepared
  // backend-readiness classification without local inference.
  // The heading text stays in its own leaf span (audit-readiness-row-label-
  // text) so existing exact-text queries against the heading keep working —
  // the badge is a sibling within the same audit-readiness-row-label cell,
  // not appended to the heading string.
  const gateKind = row.blocksRun ? "blocks" : "advisory";
  const gateLabel = gateKind === "blocks" ? "Blocks run" : "Advisory";
  const label = (
    <span className="audit-readiness-row-label">
      <span className="audit-readiness-row-label-text">{row.heading}</span>{" "}
      <span
        className={`audit-readiness-row-gate audit-readiness-row-gate--${gateKind}`}
      >
        {gateLabel}
      </span>
    </span>
  );

  if (clickable) {
    return (
      <li
        key={row.id}
        className={className}
        data-testid={row.testId}
        data-gate={gateKind}
      >
        <Button
          variant="bare"
          type="button"
          className="audit-readiness-row-btn"
          onClick={() => onSelect(row.id)}
        >
          <span className="audit-readiness-glyph" aria-hidden="true">
            {row.glyph}
          </span>
          <span className="sr-only">{row.ariaStatusLabel}.</span>
          {label}
          <span className="audit-readiness-row-summary">{row.summaryText}</span>
        </Button>
      </li>
    );
  }

  return (
    <li key={row.id} className={className} data-testid={row.testId} data-gate={gateKind}>
      <div
        className="audit-readiness-row-static"
        role="group"
        aria-label={row.heading}
      >
        <span className="audit-readiness-glyph" aria-hidden="true">
          {row.glyph}
        </span>
        <span className="sr-only">{row.ariaStatusLabel}.</span>
        {label}
        <span className="audit-readiness-row-summary">{row.summaryText}</span>
      </div>
    </li>
  );
}
