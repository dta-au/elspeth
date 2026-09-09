// src/components/chat/guided/GuidedDecisionSheet.tsx
//
// READ-ONLY record of one settled guided stage (elspeth-f2a8550b3d, slice E
// first landing). Opened from that stage's stepper tick, mounted between the
// stepper and the transcript, and built ENTIRELY from state the client
// already holds: opening a sheet issues no request, so a learner looking back
// at what they decided costs nothing and can never race the live turn.
//
// What it shows: a heading naming the stage, the components settled there
// (name + plugin display name), the stage's own chat turns replayed, and — on
// the wire stage, which settles routing rather than components — the server's
// confirmation record. Never a stable_id, never a plugin option, never a path.
//
// NO "CHANGE THIS" BUTTON. Rewinding a settled stage is lane E2 of this
// design and is deliberately absent here: whether a stage rewind is a literal
// session fork or a superseded proposal on a new composition version is an
// open operator decision, and building the control before that ruling would
// bake one answer into the UI. Each component row leaves the space for it (see
// the comment in the row) — this landing is the read-only half the ticket
// itself names as the low-risk first delivery.
//
// A11Y CONTRACT, which the tick and this component share:
//   - The tick is a disclosure button carrying aria-expanded, and
//     aria-controls pointing at THIS section's id while it is open (only one
//     sheet is mounted at a time, so a closed tick emits no aria-controls
//     rather than a dangling IDREF).
//   - The section is a named region (role=region + aria-labelledby), so the
//     panel the button controls announces what it is.
//   - Opening moves focus into the region (tabIndex={-1} on the section, not
//     a heading — a heading is not focusable), so a keyboard user lands in the
//     content they asked for; Escape and the Close button return focus to the
//     tick.
//   - The replayed turns use GuidedChatHistory's `replay` mode: a labelled
//     static group, NOT its role=log. These turns are settled history, and a
//     second live region would announce a conversation that cannot change.

import { useEffect, useId, useRef } from "react";

import { pluginDisplayName } from "@/components/catalog/pluginDisplayName";
import { Button } from "@/components/ui";
import type { ChatTurn, GuidedStep } from "@/types/guided";
import { GuidedChatHistory } from "./GuidedChatHistory";
import type { GuidedDecisionRow } from "./guidedDecisionStages";
import { GUIDED_STEP_LABELS } from "./stepLabels";

interface Props {
  /** DOM id the opening tick's `aria-controls` points at. */
  id: string;
  /** The stage this sheet records. Names the heading via GUIDED_STEP_LABELS. */
  stage: GuidedStep;
  /** Components settled at this stage, in authored order. */
  rows: readonly GuidedDecisionRow[];
  /** This stage's own chat turns, post-build conversation already excluded. */
  chatTurns: ChatTurn[];
  /**
   * The stage's own decision record from the session's audit history, or null.
   * Only the wire stage passes one — see `guidedDecisionRecord`.
   */
  record: string | null;
  /** Close and return focus to the tick that opened this sheet. */
  onClose: () => void;
}

export function GuidedDecisionSheet({
  id,
  stage,
  rows,
  chatTurns,
  record,
  onClose,
}: Props): React.ReactElement {
  const headingId = useId();
  const sectionRef = useRef<HTMLElement>(null);

  // Focus the region on open AND on a switch between stages: clicking a second
  // tick while a sheet is open re-renders this same component with a new
  // `stage`, which is an open from the user's point of view.
  useEffect(() => {
    sectionRef.current?.focus();
  }, [stage]);

  const stageLabel = GUIDED_STEP_LABELS[stage];
  const isEmpty = rows.length === 0 && chatTurns.length === 0 && record === null;

  return (
    <section
      id={id}
      ref={sectionRef}
      tabIndex={-1}
      role="region"
      aria-labelledby={headingId}
      className="guided-decision-sheet"
      onKeyDown={(event) => {
        if (event.key !== "Escape") return;
        // Stop here rather than letting the key travel: the guided surface has
        // no other Escape handler today, and a sheet that closed something
        // above it as well would be a surprise from a panel the user opened.
        event.stopPropagation();
        onClose();
      }}
    >
      <div className="guided-decision-sheet__header">
        <h3 id={headingId} className="guided-decision-sheet__heading">
          {stageLabel} — decided
        </h3>
        <Button
          variant="bare"
          className="guided-decision-sheet__close"
          onClick={onClose}
        >
          Close
        </Button>
      </div>
      {rows.length > 0 && (
        <ol className="guided-decision-sheet__components">
          {rows.map((row) => {
            // Two reasons a row shows a name alone: a structural node carries
            // no plugin at all, and a transform whose author-chosen id IS its
            // plugin name resolves to the same words twice ("Select Columns ·
            // Select Columns"), which is noise, not identification.
            const pluginLabel =
              row.plugin === null ? null : pluginDisplayName(row.plugin);
            return (
              <li key={row.key} className="guided-decision-sheet__component">
                <strong className="guided-decision-sheet__component-name">
                  {row.name}
                </strong>
                {pluginLabel !== null && pluginLabel !== row.name && (
                  <span
                    className="guided-decision-sheet__component-plugin"
                    title={row.plugin ?? undefined}
                  >
                    {pluginLabel}
                  </span>
                )}
                {/* Lane E2 mounts this row's "Change this" control here, once
                    the operator has ruled on fork-vs-supersede for a rewind. */}
              </li>
            );
          })}
        </ol>
      )}
      {record !== null && (
        <p className="guided-decision-sheet__record">{record}</p>
      )}
      {chatTurns.length > 0 && (
        <GuidedChatHistory chatHistory={chatTurns} replay />
      )}
      {isEmpty && (
        <p className="guided-decision-sheet__empty">
          Nothing was recorded at this stage.
        </p>
      )}
    </section>
  );
}
