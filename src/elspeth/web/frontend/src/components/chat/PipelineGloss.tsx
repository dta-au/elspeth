import type { JSX } from "react";
// ============================================================================
// PipelineGloss — one-line plain-language description of the pipeline (Slice C)
//
// Plain-language summary of compositionState via pipelineGloss().
// ============================================================================

import type { CompositionState } from "@/types/index";
import { pipelineGloss } from "./pipelineGloss";

interface PipelineGlossProps {
  compositionState: CompositionState | null | undefined;
}

export function PipelineGloss({
  compositionState,
}: PipelineGlossProps): JSX.Element {
  return (
    <p className="pipeline-gloss" data-testid="pipeline-gloss">
      {pipelineGloss(compositionState)}
    </p>
  );
}
