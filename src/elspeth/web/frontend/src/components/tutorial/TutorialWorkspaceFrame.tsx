import type { JSX, ReactNode } from "react";
import { PipelineValidationSummary } from "@/components/chat/PipelineValidationSummary";
import { ArtifactWorkspace } from "@/components/workspace/ArtifactWorkspace";
import { ComposerWorkspace } from "@/components/workspace/ComposerWorkspace";
import { WorkspaceActionBar } from "@/components/workspace/WorkspaceActionBar";

/**
 * The step header: the step's title, the one instruction that says what the
 * learner should do next, and the step's forward action. It spans BOTH panes
 * above the workspace, so it is visible at every width, in either narrow view,
 * and with the authoring pane collapsed. The workspace's own bottom bar is not
 * an option for the forward action: its artifact cell is hidden in narrow
 * Compose view (ComposerWorkspace `hidden={artifactViewHidden}`).
 */
export interface TutorialStepHeader {
  title: string;
  /** The instruction line; give it an id when an action is described by it. */
  instruction: ReactNode;
  /** Alerts tied to the step's action (e.g. a refused readiness check). */
  notice?: ReactNode;
  actions?: ReactNode;
}

interface TutorialWorkspaceFrameProps {
  /** Accessible name of the frame's landmark (`section`). */
  ariaLabel: string;
  /** Step header above both panes. Run keeps its heading in its card. */
  header?: TutorialStepHeader;
  /** The authoring pane's content: the freeform ChatPanel, or the run card. */
  children: ReactNode;
}

/**
 * The tutorial's workspace frame: the real ComposerWorkspace with the
 * artifact (graph / YAML / checks / run) and inspector panes wired exactly as
 * the app wires them, minus the completion action bar (the tutorial mounts
 * no REQUEST_RUN_EVENT owner — elspeth-553a6fb81d). ONE definition, used by
 * both the freeform build step and the run step, so the run turn keeps the
 * pipeline pane the learner just confirmed in (I-1: "show the graph and a Run
 * button") rather than replacing the whole workspace with a bare card.
 *
 * Tutorial chrome lives in the step header, never inside the authoring pane:
 * anything stacked around the ChatPanel there shrinks the conversation's
 * min(160px, 30%) floor (chat.css) and pushes the chat header off the row it
 * shares with the artifact toolbar across the pane seam.
 *
 * The panes read the session store; the caller binds it before display.
 */
export function TutorialWorkspaceFrame({
  ariaLabel,
  header,
  children,
}: TutorialWorkspaceFrameProps): JSX.Element {
  return (
    <section className="tutorial-workspace-shell" aria-label={ariaLabel}>
      {header !== undefined && (
        <header className="tutorial-step-header">
          <div className="tutorial-step-header-text">
            <h2>{header.title}</h2>
            {header.instruction}
            {header.notice}
          </div>
          {header.actions !== undefined && (
            <div className="tutorial-step-header-actions">{header.actions}</div>
          )}
        </header>
      )}
      <ComposerWorkspace
        authoring={children}
        artifact={
          <ArtifactWorkspace
            checksValidationContent={<PipelineValidationSummary isTutorial />}
          />
        }
        actionBar={
          <WorkspaceActionBar
            capabilities={{
              completion: false,
            }}
          />
        }
      />
    </section>
  );
}
