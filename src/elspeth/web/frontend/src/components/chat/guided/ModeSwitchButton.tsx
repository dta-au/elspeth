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
// reenterGuided, while any other session gets a FRESH wizard rooted on a goal
// the user states here (the current pipeline is saved to version history
// first). The confirm copy must name whichever of those the click will
// actually do — identical alarming copy for the safe resume made users refuse
// a safe action.
//
// The FRESH-wizard direction always shows the card, `hasWork` or not
// (goal-first, elspeth-378cfa0e18): the new wizard needs a goal to be rooted
// on, and that goal has to be typed somewhere. The store cannot tell an empty
// session from a worked one synchronously — the discriminator is the server's
// GET /guided answer — so the card asks once and `enterGuided(goal)` routes it:
// an empty session starts with it, a worked one converts with it. The old
// single-click switch for an empty chat is gone with the rootless wizard it
// created. RESUME keeps the light two-step confirm gated on `hasWork`: it
// re-opens a wizard that already has a root, so there is nothing to ask for.
//
// `hasWork` is computed once by ChatPanel (messages / guided turns / a non-empty
// composition) and passed in, so this component needs no store-shape knowledge
// beyond the two switch actions and the resume/fresh discriminator read below.
// ============================================================================

import { useEffect, useId, useState } from "react";

import { Button } from "@/components/ui";
import { useSessionStore } from "@/stores/sessionStore";

interface ModeSwitchButtonProps {
  target: "guided" | "freeform";
  hasWork: boolean;
  /** Optional caller-owned explanation for a disabled mode transition. */
  disabledReason?: string;
}

/**
 * The goal box's own copy. A placeholder is NOT a label (it disappears on the
 * first keystroke and is not an accessible name), so the question is a real
 * <label> and the example rides in the placeholder — the same split the goal
 * card in ChatPanel uses.
 */
const GOAL_LABEL = "What should this pipeline produce?";
const GOAL_PLACEHOLDER =
  "In one sentence: what should come out the other end — e.g. a summary per page, saved as JSON…";

export function ModeSwitchButton({
  target,
  hasWork,
  disabledReason,
}: ModeSwitchButtonProps): JSX.Element {
  const [confirming, setConfirming] = useState(false);
  const [goal, setGoal] = useState("");
  const enterGuided = useSessionStore((s) => s.enterGuided);
  const exitToFreeform = useSessionStore((s) => s.exitToFreeform);
  // F10b: the same predicate enterGuided branches on — exited-guided sessions
  // RESUME their saved wizard (reenterGuided); everything else converts to a
  // fresh wizard. The confirm copy must describe the path this click takes.
  const resumesSavedGuidedSession = useSessionStore(
    (s) => s.guidedSession?.terminal?.kind === "exited_to_freeform",
  );
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  // The goal belongs to the session it was typed for, but this component is
  // NOT remounted when the user switches sessions — ChatPanel is mounted
  // without a key, so React reconciles to the same instance and both the typed
  // goal and the open card survive the switch. Left as-is, a goal typed (or
  // abandoned with Cancel) on session A prefills session B's card with Confirm
  // already enabled, and one click roots B on it: B's durable route_user_message
  // row, B's first transcript turn, and B's planner brief, all carrying words
  // the author never wrote for B. Clearing on the session id is the fix that
  // does not disturb any other ChatPanel state.
  useEffect(() => {
    setGoal("");
    setConfirming(false);
  }, [activeSessionId]);
  const reactId = useId();
  const disabledReasonId = `${reactId}-mode-switch-disabled-reason`;
  const confirmDescriptionId = `${reactId}-mode-switch-confirm-description`;
  const goalFieldId = `${reactId}-mode-switch-goal`;

  // The fresh-wizard direction: the one that needs a goal, and the one whose
  // card is unconditional.
  const collectsGoal = target === "guided" && !resumesSavedGuidedSession;
  const trimmedGoal = goal.trim();

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
        : "Guided mode starts a fresh pipeline from the goal you state here. Your current pipeline is saved to version history and can be restored."
      : "Your guided progress remains saved. You can continue in the freeform composer with the current pipeline context.";

  function doSwitch(): void {
    if (target === "freeform") {
      void exitToFreeform();
      return;
    }
    // Resume carries no goal — the saved wizard already has its root.
    void enterGuided(collectsGoal ? trimmedGoal : undefined);
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
        {collectsGoal && (
          <label className="mode-switch-goal" htmlFor={goalFieldId}>
            {GOAL_LABEL}
            <textarea
              id={goalFieldId}
              className="textarea mode-switch-goal-input"
              value={goal}
              onChange={(event) => setGoal(event.target.value)}
              placeholder={GOAL_PLACEHOLDER}
              rows={3}
              required
            />
          </label>
        )}
        <Button
          variant="bare"
          className="mode-switch-btn mode-switch-btn--confirm"
          aria-describedby={confirmDescriptionId}
          // A goal-less confirm has nothing to root the new wizard on, and the
          // store would refuse it. Disable rather than submit-and-explain.
          disabled={collectsGoal && trimmedGoal === ""}
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
      onClick={() =>
        collectsGoal || hasWork ? setConfirming(true) : doSwitch()
      }
    >
      {label}
    </Button>
  );
}
