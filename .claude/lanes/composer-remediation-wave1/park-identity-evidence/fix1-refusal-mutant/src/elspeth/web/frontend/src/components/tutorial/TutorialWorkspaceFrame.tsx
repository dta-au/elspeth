import type { JSX, ReactNode } from "react";
import { PipelineValidationSummary } from "@/components/chat/PipelineValidationSummary";
import { ArtifactWorkspace } from "@/components/workspace/ArtifactWorkspace";
import { ComposerWorkspace } from "@/components/workspace/ComposerWorkspace";
import { WorkspaceActionBar } from "@/components/workspace/WorkspaceActionBar";
import { TutorialStepHeader, type TutorialStepHeaderProps } from "./TutorialStepHeader";

interface TutorialWorkspaceFrameProps {
  /** Accessible name of the frame's landmark (`section`). */
  ariaLabel: string;
  /** The step header, on a band above both panes. */
  header: TutorialStepHeaderProps;
  /** The authoring pane's content: the freeform ChatPanel, or the run's body. */
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
 * min(160px, 30%) floor (chat.css) and pushes the conversation below the top
 * of the artifact pane beside it.
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
      <TutorialStepHeader {...header} />
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
