import type { ReactNode } from "react";
import { PipelineValidationSummary } from "@/components/chat/guided/PipelineValidationSummary";
import { ArtifactWorkspace } from "@/components/workspace/ArtifactWorkspace";
import { ComposerWorkspace } from "@/components/workspace/ComposerWorkspace";
import { WorkspaceActionBar } from "@/components/workspace/WorkspaceActionBar";
import { WorkspaceInspector } from "@/components/workspace/WorkspaceInspector";

interface TutorialWorkspaceFrameProps {
  /** Accessible name of the frame's landmark (`section`). */
  ariaLabel: string;
  /** The authoring pane's content: the guided ChatPanel, or the run card. */
  children: ReactNode;
}

/**
 * The tutorial's workspace frame: the real ComposerWorkspace with the
 * artifact (graph / YAML / checks / run) and inspector panes wired exactly as
 * the app wires them, minus the completion action bar (the tutorial mounts
 * no REQUEST_RUN_EVENT owner — elspeth-553a6fb81d). ONE definition, used by
 * both the guided build step and the run step, so the run turn keeps the
 * pipeline pane the learner just confirmed in (I-1: "show the graph and a Run
 * button") rather than replacing the whole workspace with a bare card.
 *
 * The panes read the session store; the caller binds it (the guided shell's
 * start effect, or HelloWorldTutorial's resume re-bind for the run step).
 */
export function TutorialWorkspaceFrame({
  ariaLabel,
  children,
}: TutorialWorkspaceFrameProps): JSX.Element {
  return (
    <section className="tutorial-guided-shell" aria-label={ariaLabel}>
      <ComposerWorkspace
        authoring={children}
        artifact={
          <ArtifactWorkspace
            checksValidationContent={<PipelineValidationSummary isTutorial />}
          />
        }
        inspector={<WorkspaceInspector />}
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
