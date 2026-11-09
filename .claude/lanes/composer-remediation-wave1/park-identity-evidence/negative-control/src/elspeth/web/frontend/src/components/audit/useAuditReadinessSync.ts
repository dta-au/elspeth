import { useEffect } from "react";

import { matchingAuditReadinessSnapshot } from "@/lib/auditReadinessFreshness";
import { useAuditReadinessStore } from "@/stores/auditReadinessStore";
import { useExecutionStore } from "@/stores/executionStore";
import { useSessionStore } from "@/stores/sessionStore";
import type { AuditReadinessSnapshot, ValidationResult } from "@/types/api";
import { hasCompositionContent } from "@/utils/compositionState";

function validationResultFromSnapshot(
  snapshot: AuditReadinessSnapshot,
): ValidationResult {
  return snapshot.validation_result;
}

/** Apply a freshly loaded snapshot's validation result to the execution
 *  store — but only when the snapshot still matches the ACTIVE session and
 *  composition version at apply time (a fetch can resolve after the user has
 *  moved on). Shared by the ambient sync below and the panel's manual
 *  refresh affordances. */
export function projectMatchingSnapshotToExecution(
  sessionId: string,
  compositionVersion: number,
  setValidationResult: (result: ValidationResult) => void,
): void {
  const currentSnapshot =
    useAuditReadinessStore.getState().snapshotsBySession[sessionId];
  const activeSessionId = useSessionStore.getState().activeSessionId;
  const activeVersion =
    useSessionStore.getState().compositionState?.version ?? null;
  if (
    activeSessionId !== sessionId ||
    activeVersion !== compositionVersion ||
    matchingAuditReadinessSnapshot(
      currentSnapshot,
      sessionId,
      compositionVersion,
    ) === undefined
  ) {
    return;
  }
  setValidationResult(validationResultFromSnapshot(currentSnapshot));
}

/** Keep the audit-readiness snapshot fresh for the active composition.
 *
 *  Historically this effect lived only inside AuditReadinessPanel, which was
 *  mounted (hidden) at all times by the Inspector — so the snapshot behind
 *  the action-bar Audit chip was always ambient. With the panel now mounted
 *  only while the Checks tab is active, the always-mounted
 *  ArtifactWorkspaceSurface runs this hook too (gated off while the panel's
 *  own instance is mounted, so exactly one instance syncs at a time) — the
 *  Checks-tab badge must settle without the tab ever being visited.
 *
 *  `loadSnapshot` early-returns on a fresh matching snapshot, so handing the
 *  sync from one instance to the other never refetches settled state. */
export function useAuditReadinessSync(enabled = true): void {
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const compositionVersion = useSessionStore(
    (s) => s.compositionState?.version ?? null,
  );
  const compositionHasContent = useSessionStore((s) =>
    hasCompositionContent(s.compositionState),
  );
  const loadSnapshot = useAuditReadinessStore((s) => s.loadSnapshot);
  const setValidationResult = useExecutionStore((s) => s.setValidationResult);

  useEffect(() => {
    if (
      !enabled ||
      !activeSessionId ||
      compositionVersion === null ||
      !compositionHasContent
    ) {
      return;
    }
    let cancelled = false;
    // Fire and forget; store handles errors.
    void loadSnapshot(activeSessionId, compositionVersion).then(() => {
      if (cancelled) return;
      projectMatchingSnapshotToExecution(
        activeSessionId,
        compositionVersion,
        setValidationResult,
      );
    });
    return () => {
      cancelled = true;
      // Unmount-during-fetch cleanup: abort the in-flight controller for this
      // session. The store's AbortError catch arm clears
      // isLoadingBySession[activeSessionId] and preserves cached snapshot/error.
      const ctrl =
        useAuditReadinessStore.getState().abortControllers[activeSessionId];
      if (ctrl) {
        ctrl.abort();
      }
    };
  }, [
    activeSessionId,
    compositionHasContent,
    compositionVersion,
    enabled,
    loadSnapshot,
    setValidationResult,
  ]);
}
