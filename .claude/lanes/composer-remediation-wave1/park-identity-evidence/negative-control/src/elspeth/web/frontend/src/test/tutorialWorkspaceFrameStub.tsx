import type { JSX, ReactNode } from "react";
import { TutorialStepHeader, type TutorialStepHeaderProps } from "@/components/tutorial/TutorialStepHeader";

/**
 * Test stand-in for TutorialWorkspaceFrame, for suites that exercise a
 * tutorial step's own behaviour:
 *
 *   vi.mock("./TutorialWorkspaceFrame", () => import("@/test/tutorialWorkspaceFrameStub"));
 *
 * It renders the REAL step header (title, instruction, notice, actions) and
 * the authoring content, and leaves out the store-bound workspace panes
 * (ComposerWorkspace needs ResizeObserver and a bound session). The real
 * frame is covered by the a11y suite and tests/e2e/tutorial.spec.ts.
 */
export function TutorialWorkspaceFrame({
  ariaLabel,
  header,
  children,
}: {
  ariaLabel: string;
  header: TutorialStepHeaderProps;
  children: ReactNode;
}): JSX.Element {
  return (
    <section data-testid="tutorial-workspace-frame" aria-label={ariaLabel}>
      <TutorialStepHeader {...header} />
      <div data-testid="authoring-pane">{children}</div>
    </section>
  );
}
