// src/components/inspector/GuidedGraphPane.tsx
//
// The Pipeline pane's pre-commit drawing for a guided build
// (elspeth-9f0873426a, IA-1 / V-1). GraphView renders this in place of its
// "No pipeline to visualise" empty state whenever guidedGraphProjection has
// something to show — the reviewed source/output, the pending proposal, or
// the pending wire stage — so the learner sees the structure they are
// deciding on in the wide pane rather than in a 300px card. Presentation
// only: the projection is the single authority for what is drawn.

import type { JSX } from "react";
import type { GuidedGraphProjection } from "@/components/chat/guided/guidedGraphProjection";
import { ReadOnlyPipelineGraph } from "@/components/chat/guided/ReadOnlyPipelineGraph";
import { Button } from "@/components/ui/Button";

export function GuidedGraphPane({
  projection,
  onFullscreen,
}: {
  projection: GuidedGraphProjection;
  onFullscreen?: () => void;
}): JSX.Element {
  return (
    <div className="graph-view-guided" data-guided-stage={projection.stage}>
      <p className="graph-view-guided__caption">{projection.caption}</p>
      {onFullscreen && (
        <div>
          <Button compact onClick={onFullscreen} title="Fullscreen">Fullscreen</Button>
        </div>
      )}
      <ReadOnlyPipelineGraph
        nodes={projection.nodes}
        edges={projection.edges}
        ariaLabel={projection.ariaLabel}
      />
    </div>
  );
}
