import { matchingAuditReadinessSnapshot } from "@/lib/auditReadinessFreshness";
import type {
  AuditReadinessSnapshot,
  ValidationResult,
} from "@/types/api";
import { plural } from "@/utils/plural";

export type WorkspaceStatusTone =
  | "neutral"
  | "busy"
  | "success"
  | "warning"
  | "error";

export interface WorkspaceStatus {
  text: string;
  tone: WorkspaceStatusTone;
  accessibleLabel: string;
  /** This channel's contribution to the merged Checks count: concrete
   *  findings behind an error/warning tone that no other channel already
   *  carries (0 for every other tone, and for zero-count failures like a
   *  failed check request). The merge projection sums it across channels, so
   *  a fact two channels both know — the audit snapshot's "validation" row
   *  mirrors the validation channel's own failure — counts in exactly one of
   *  them. It may therefore undercount a channel's standalone `text`. */
  issueCount: number;
}

function validationStatus(
  text: string,
  tone: WorkspaceStatusTone,
  issueCount = 0,
): WorkspaceStatus {
  return {
    text,
    tone,
    accessibleLabel: `Validation: ${text}`,
    issueCount,
  };
}

export function projectValidationWorkspaceStatus(
  validationResult: ValidationResult | null,
  isValidating = false,
  validationError: string | null = null,
): WorkspaceStatus {
  if (isValidating) return validationStatus("Checking", "busy");
  if (validationError !== null) {
    return validationStatus("Check failed", "error");
  }
  if (validationResult === null) {
    return validationStatus("Not checked", "neutral");
  }
  const errorCount = validationResult.errors.length;
  if (errorCount > 0) {
    return validationStatus(plural(errorCount, "error"), "error", errorCount);
  }
  if (!validationResult.is_valid) return validationStatus("Failed", "error");
  const warningCount = validationResult.warnings?.length ?? 0;
  if (warningCount > 0) {
    return validationStatus(
      plural(warningCount, "warning"),
      "warning",
      warningCount,
    );
  }
  return validationStatus("Passed", "success");
}

interface AuditWorkspaceStatusInputs {
  activeSessionId: string | null;
  compositionVersion: number | null;
  snapshotsBySession: Record<string, AuditReadinessSnapshot>;
  errorBySession: Record<string, string | null>;
}

function auditStatus(
  text: string,
  tone: WorkspaceStatusTone,
  issueCount = 0,
): WorkspaceStatus {
  return { text, tone, accessibleLabel: `Audit: ${text}`, issueCount };
}

export function projectAuditWorkspaceStatus({
  activeSessionId,
  compositionVersion,
  snapshotsBySession,
  errorBySession,
}: AuditWorkspaceStatusInputs): WorkspaceStatus {
  const error =
    activeSessionId === null
      ? null
      : errorBySession[activeSessionId] ?? null;
  if (error !== null) return auditStatus("Error", "error");

  const cached =
    activeSessionId === null
      ? undefined
      : snapshotsBySession[activeSessionId];
  const snapshot = matchingAuditReadinessSnapshot(
    cached,
    activeSessionId,
    compositionVersion,
  );
  if (snapshot === undefined) return auditStatus("Checking", "busy");

  const expectedIds = new Set([
    "validation", "plugin_trust", "provenance", "retention",
    "llm_interpretations", "secrets",
  ]);
  const rowIds = snapshot.rows.map((row) => row.id);
  const validationRow = snapshot.rows.find((row) => row.id === "validation");
  if (
    rowIds.length !== expectedIds.size ||
    new Set(rowIds).size !== expectedIds.size ||
    rowIds.some((id) => !expectedIds.has(id)) ||
    validationRow === undefined ||
    (!snapshot.validation_result.is_valid && validationRow.status !== "error") ||
    (snapshot.validation_result.is_valid && validationRow.status === "error")
  ) {
    return auditStatus("Error", "error");
  }

  const issueRows = snapshot.rows.filter(
    (row) => row.status === "warning" || row.status === "error",
  );
  if (issueRows.length === 0) return auditStatus("Ready", "success");
  const tone = issueRows.some((row) => row.status === "error")
    ? "error"
    : "warning";
  // The "validation" row is mechanically tied to validation_result.is_valid
  // (the consistency guard above enforces it), so it restates a failure the
  // validation channel already counts error-by-error. Text and tone keep it
  // — standalone, "2 issues" over two red rows is the honest reading — but
  // the merge contribution excludes it or every validation failure would
  // inflate the merged Checks count by exactly one.
  const contributed = issueRows.filter((row) => row.id !== "validation").length;
  return auditStatus(plural(issueRows.length, "issue"), tone, contributed);
}

/* Ambient severity order for the merged Checks projection. Error outranks
   warning because validation errors block running; busy outranks the resting
   tones so an in-flight check never reads as a settled verdict; and neutral
   ("Not checked") outranks success because green must only ever mean "both
   channels checked and clean". */
const CHECKS_TONE_SEVERITY: readonly WorkspaceStatusTone[] = [
  "error",
  "warning",
  "busy",
  "neutral",
  "success",
];

/** Merge the validation and audit projections into the single ambient status
 *  the Checks artifact tab presents: worst-of tone, summed finding count, and
 *  a deliberately jargon-free text (the per-channel wording lives inside the
 *  Checks panel itself, one selection away). */
export function projectChecksWorkspaceStatus(
  validation: WorkspaceStatus,
  audit: WorkspaceStatus,
): WorkspaceStatus {
  const tone = CHECKS_TONE_SEVERITY.find(
    (candidate) => validation.tone === candidate || audit.tone === candidate,
  ) as WorkspaceStatusTone;
  const issueCount = validation.issueCount + audit.issueCount;
  const text =
    issueCount > 0
      ? plural(issueCount, "issue")
      : tone === "error"
        ? "Check failed"
        : tone === "warning"
          ? "Review"
          : tone === "busy"
            ? "Checking"
            : tone === "neutral"
              ? "Not checked"
              : "Ready";
  return { text, tone, accessibleLabel: `Checks: ${text}`, issueCount };
}
