import { type ReactNode, useCallback } from "react";

import { AuditReadinessPanel } from "@/components/audit/AuditReadinessPanel";
import { SideRailValidationBanner } from "@/components/sidebar/SideRailValidationBanner";
import { dispatchArtifactViewIntent } from "@/lib/composer-events";
import { useSessionStore } from "@/stores/sessionStore";

/** The Checks artifact tab: the one home for both pipeline assessments —
 *  validation findings and audit readiness — rendered inline where the old
 *  action-bar chips only linked to an Inspector drawer. Both panels are the
 *  same components that drawer mounted; this is a relocation, not a fork.
 *
 *  `validationContent` exists for the tutorial shell, which projects its own
 *  PipelineValidationSummary — a content override on the shared surface, not
 *  a tutorial-only code path (the tab, panel, and audit surface are
 *  identical in every mount). */
export function ChecksView({
  validationContent,
}: {
  validationContent?: ReactNode;
} = {}): JSX.Element {
  const activeSessionId = useSessionStore((state) => state.activeSessionId);
  const selectComponent = useCallback(
    (componentId: string): void => {
      useSessionStore.getState().selectNode(componentId);
      dispatchArtifactViewIntent({
        tab: "graph",
        focusMode: false,
        sessionId: activeSessionId,
      });
    },
    [activeSessionId],
  );

  return (
    <div className="checks-view">
      {/* Named landmark, mirroring AuditReadinessPanel's own
          <section aria-label="Audit readiness">: the banner renders no
          heading or label of its own, and the Inspector tabpanel that used
          to name this content "Validation" retired with the drawer. */}
      <section aria-label="Validation">
        {validationContent === undefined ? (
          <SideRailValidationBanner onSelectComponent={selectComponent} />
        ) : (
          validationContent
        )}
      </section>
      <AuditReadinessPanel onSelectComponent={selectComponent} />
    </div>
  );
}
