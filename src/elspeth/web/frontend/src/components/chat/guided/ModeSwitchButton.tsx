// ============================================================================
// ModeSwitchButton — the symmetric guided<->freeform mode toggle.
//
// One component for BOTH directions:
//   target="guided"   -> "Switch to guided"  (freeform body header)
//   target="freeform" -> "Exit to freeform"  (guided body header)
//
// guided -> freeform (exitToFreeform) is always non-destructive: the server
// retains the guided state on the session. freeform -> guided (enterGuided)
// BRANCHES on the session's history (F10b): a session that previously exited
// guided (terminal.kind === "exited_to_freeform") RESUMES its saved wizard via
// reenterGuided, while any other session gets a FRESH wizard via convert (the
// current pipeline is saved to version history first). The confirm copy must
// name whichever of those the click will actually do — identical alarming
// copy for the safe resume made users refuse a safe action.
// Because a stray click still yanks the user out of an in-progress chat, the
// switch is gated by a light two-step confirm WHEN the chat has work; an empty
// chat switches on a single click.
//
// `hasWork` is computed once by ChatPanel (messages / guided turns / a non-empty
// composition) and passed in, so this component needs no store-shape knowledge
// beyond the two switch actions and the resume/fresh discriminator read below.
// ============================================================================

import { useId, useState } from "react";

import { Button } from "@/components/ui";
import { useSessionStore } from "@/stores/sessionStore";

interface ModeSwitchButtonProps {
  target: "guided" | "freeform";
  hasWork: boolean;
  /** Optional caller-owned explanation for a disabled mode transition. */
  disabledReason?: string;
}

export function ModeSwitchButton({
  target,
  hasWork,
  disabledReason,
}: ModeSwitchButtonProps): JSX.Element {
  const [confirming, setConfirming] = useState(false);
  const enterGuided = useSessionStore((s) => s.enterGuided);
  const exitToFreeform = useSessionStore((s) => s.exitToFreeform);
  // F10b: the same predicate enterGuided branches on — exited-guided sessions
  // RESUME their saved wizard (reenterGuided); everything else converts to a
  // fresh wizard. The confirm copy must describe the path this click takes.
  const resumesSavedGuidedSession = useSessionStore(
    (s) => s.guidedSession?.terminal?.kind === "exited_to_freeform",
  );
  const reactId = useId();
  const disabledReasonId = `${reactId}-mode-switch-disabled-reason`;
  const confirmDescriptionId = `${reactId}-mode-switch-confirm-description`;

  const label = target === "guided" ? "Switch to guided" : "Exit to freeform";
  const confirmLabel =
    target === "guided"
      ? "Confirm switch to guided"
      : "Confirm exit to freeform";
  const confirmTitle =
    target === "guided" ? "Switch to guided mode?" : "Exit to freeform mode?";
  const confirmNote =
    target === "guided"
      ? resumesSavedGuidedSession
        ? "You'll pick up your guided setup where you left it. Nothing is discarded."
        : "Guided mode starts a fresh pipeline. Your current pipeline is saved to version history and can be restored."
      : "Your guided progress remains saved. You can continue in the freeform composer with the current pipeline context.";

  function doSwitch(): void {
    void (target === "guided" ? enterGuided() : exitToFreeform());
  }

  if (disabledReason !== undefined) {
    return (
      <div className="mode-switch-disabled">
        <Button
          variant="bare"
          className="mode-switch-btn"
          disabled
          aria-describedby={disabledReasonId}
        >
          {label}
        </Button>
        <span id={disabledReasonId} className="mode-switch-disabled-reason">
          {disabledReason}
        </span>
      </div>
    );
  }

  if (confirming) {
    return (
      <div
        className="mode-switch-confirm mode-switch-confirm-card"
        role="group"
        aria-label={confirmLabel}
        aria-describedby={confirmDescriptionId}
      >
        <span className="mode-switch-confirm-title">{confirmTitle}</span>
        <span id={confirmDescriptionId} className="mode-switch-confirm-note">
          {confirmNote}
        </span>
        <Button
          variant="bare"
          className="mode-switch-btn mode-switch-btn--confirm"
          aria-describedby={confirmDescriptionId}
          onClick={() => {
            setConfirming(false);
            doSwitch();
          }}
        >
          {confirmLabel}
        </Button>
        <Button
          variant="bare"
          className="mode-switch-btn"
          onClick={() => setConfirming(false)}
        >
          Cancel
        </Button>
      </div>
    );
  }

  return (
    <Button
      variant="bare"
      className="mode-switch-btn"
      onClick={() => (hasWork ? setConfirming(true) : doSwitch())}
    >
      {label}
    </Button>
  );
}
