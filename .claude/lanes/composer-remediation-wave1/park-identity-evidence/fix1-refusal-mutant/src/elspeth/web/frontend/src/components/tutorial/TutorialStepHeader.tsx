import type { JSX, ReactNode, RefObject } from "react";

/**
 * A workspace step's header: its title, the one instruction that says what
 * the learner should do next, any alert about the step's action, and the
 * step's forward action. TutorialWorkspaceFrame renders it on a band that
 * spans both panes, so it is visible at every width, in either narrow view,
 * and with the authoring pane collapsed. The workspace's own bottom bar is not
 * an option for the forward action: its artifact cell is hidden in narrow
 * Compose view (ComposerWorkspace `hidden={artifactViewHidden}`).
 */
export interface TutorialStepHeaderProps {
  title: string;
  /**
   * Focus target for a step that moves focus to its title when it mounts or
   * changes phase; the heading becomes programmatically focusable (tabIndex
   * -1) without entering the tab order.
   */
  headingRef?: RefObject<HTMLHeadingElement>;
  /** The instruction line; give it an id when an action is described by it. */
  instruction: ReactNode;
  /** Alerts tied to the step's action (e.g. a refused readiness check). */
  notice?: ReactNode;
  actions?: ReactNode;
}

export function TutorialStepHeader({
  title,
  headingRef,
  instruction,
  notice,
  actions,
}: TutorialStepHeaderProps): JSX.Element {
  return (
    <header className="tutorial-step-header">
      <div className="tutorial-step-header-text">
        <h2 ref={headingRef} tabIndex={headingRef === undefined ? undefined : -1}>
          {title}
        </h2>
        {instruction}
        {notice}
      </div>
      {actions !== undefined && (
        <div className="tutorial-step-header-actions">{actions}</div>
      )}
    </header>
  );
}
