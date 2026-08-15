// ============================================================================
// AcknowledgementCard.tsx — one LLM-authored decision, reframed as an
// acknowledgement (not an approval gate).
//
// Extracted from the retired InterpretationReviewTurn / InterpretationReview-
// InlineMessage presentation.  The behaviour — resolve / amend / error
// mapping / 8 KB cap — is reused VERBATIM via `useInterpretationResolver`;
// this component owns only the compact card rendering, the per-kind copy, the
// value rendering (shared CodeBlock for JSON; monospace prompt template behind
// a "View prompt" disclosure that gates a separate Approve), ARIA wiring, and
// focus on amend toggle.
//
// Acknowledge == today's accept (`accepted_as_drafted`).  The card NEVER
// auto-steals focus on mount (the stack is persistent and must not yank focus
// from someone typing in the chat input); the stack announces instead.
// ============================================================================

import {
  useEffect,
  useId,
  useRef,
  useState,
  type CSSProperties,
  type ReactNode,
} from "react";
import type { CompositionState } from "@/types/index";
import type { InterpretationEvent } from "@/types/interpretation";
import { Button } from "@/components/ui";
import { CodeBlock } from "./CodeBlock";
import { resolvePromptDisplaySegments } from "./promptTemplateDisplay";
import {
  ACKNOWLEDGEMENT_ACCEPT_LABEL,
  ACKNOWLEDGEMENT_AMEND_LABEL,
  ACKNOWLEDGEMENT_APPROVE_LABEL,
  ACKNOWLEDGEMENT_VIEW_PROMPT_LABEL,
} from "./acknowledgementLabels";
import {
  INTERPRETATION_AMENDMENT_MAX_BYTES,
  useInterpretationResolver,
} from "@/hooks/useInterpretationResolver";

const PROMPT_TEMPLATE_STYLE: CSSProperties = {
  maxHeight: "16rem",
  overflow: "auto",
};

/**
 * Stable DOM id for a card's labelled <section>. The wire-stage named-blocker
 * links (WireStageTurn) target this id to scroll to + focus the blocking card
 * from the other column (elspeth-3b35abf148 variant 1).
 */
export function acknowledgementCardDomId(eventId: string): string {
  return `ack-card-${eventId}`;
}

/**
 * Scroll the card into view and move focus to its section (tabIndex=-1).
 * Deliberate focus-steal: unlike mount-time announce-don't-steal, this runs
 * only on an explicit user click of a "go to blocker" link.
 */
export function focusAcknowledgementCard(eventId: string): void {
  const element = document.getElementById(acknowledgementCardDomId(eventId));
  if (element === null) return;
  element.scrollIntoView({ behavior: "smooth", block: "center" });
  element.focus({ preventScroll: true });
}

function assertNever(value: never): never {
  throw new Error(`Unhandled interpretation kind: ${String(value)}`);
}

interface CardPresentation {
  /** Title row: humanised step label · kind (e.g. "Summarise step · model"). */
  title: string;
  /** One punchy, LLM-attributed line. */
  line: ReactNode;
  /** Accessible label for the Acknowledge button (names the decision). */
  acceptAriaLabel: string;
}

function getCardPresentation(
  event: InterpretationEvent,
  stepLabel: string,
): CardPresentation {
  const userTerm = event.user_term ?? "this term";
  const llmDraft = event.llm_draft ?? "";
  switch (event.kind) {
    case "llm_prompt_template":
      return {
        title: `${stepLabel} step · prompt`,
        line: "The LLM wrote the instruction for this step.",
        // Prompt cards use the two-stage View→Approve button, so the accept
        // action is named "Approve" (visible label and accessible name must
        // agree — WCAG 2.5.3 label-in-name).
        acceptAriaLabel: "Approve the LLM prompt template",
      };
    case "pipeline_decision":
      return {
        title: `${stepLabel} step · decision`,
        line: (
          <span className="ack-card-decision">
            {llmDraft || "(no decision recorded)"}
          </span>
        ),
        acceptAriaLabel: "Acknowledge the pipeline decision",
      };
    case "llm_model_choice":
      return {
        title: `${stepLabel} step · model`,
        line: (
          <>
            The LLM picked{" "}
            <code className="ack-card-model">{llmDraft || "(unspecified)"}</code>
            .
          </>
        ),
        acceptAriaLabel: "Acknowledge the LLM model choice",
      };
    case "invented_source":
      return {
        title: "Source data",
        line: "The LLM invented this source data — review before fetching.",
        acceptAriaLabel: "Acknowledge the invented source data",
      };
    case "vague_term":
    case null:
      return {
        title: "Interpretation",
        line: (
          <>
            You said{" "}
            <em className="ack-card-user-term">{userTerm}</em>; the LLM read it
            as{" "}
            <em className="ack-card-llm-draft">{llmDraft}</em>.
          </>
        ),
        acceptAriaLabel: `Acknowledge the LLM's interpretation of ${userTerm}`,
      };
    default:
      return assertNever(event.kind);
  }
}

/**
 * The card's plain-string title ("Summarise step · prompt"). Exported so the
 * wire-stage blocker list can name the pending card it links to using the
 * exact same wording the card renders — a blocker label that disagreed with
 * the card title would be a fresh way to get lost.
 */
export function acknowledgementCardTitle(
  event: InterpretationEvent,
  stepLabel: string,
): string {
  return getCardPresentation(event, stepLabel).title;
}

export interface AcknowledgementCardProps {
  /** The pending interpretation event to acknowledge. */
  event: InterpretationEvent;
  /** Owning session id; round-tripped to the store actions. */
  sessionId: string;
  /** Humanised step label resolved from the composition (e.g. "Summarise"). */
  stepLabel: string;
  /**
   * The session's live composition state (threaded from the stack's existing
   * store subscription — the card itself stays store-free and testable).
   * Prompt-template cards use it to render the prompt with accepted
   * interpretation values substituted (elspeth-990f5ea562); the event's
   * `llm_draft` is frozen at staging time with every slot masked as
   * "pending interpretation".  Optional: without it the card falls back to
   * the frozen draft.
   */
  compositionState?: CompositionState | null;
  /** Render the inline amend affordance (vague_term only; off in tutorial). */
  showAmend?: boolean;
  /**
   * Fired after a successful resolve so the parent can advance its surface.
   * Errors do NOT fire onResolved — the card stays mounted with an error
   * banner.
   */
  onResolved?: (newState: CompositionState | null) => void;
  /**
   * Callback ref for the Acknowledge button.  The stack uses it to restore
   * focus to the NEXT card's primary action after this card resolves and
   * unmounts (so a keyboard / SR user is not stranded at document.body).
   * Null while the card is in amend mode (no Acknowledge button rendered).
   */
  acceptButtonRef?: (el: HTMLButtonElement | null) => void;
  /**
   * Callback ref for the card's labelled <section> (tabIndex=-1).  Used by the
   * stack as a focus fallback when the next card's primary button is absent
   * or disabled (e.g. amend mode, or a resolve in flight).
   */
  sectionRef?: (el: HTMLElement | null) => void;
}

export function AcknowledgementCard({
  event,
  sessionId,
  stepLabel,
  compositionState = null,
  showAmend = false,
  onResolved,
  acceptButtonRef,
  sectionRef,
}: AcknowledgementCardProps) {
  const {
    mode,
    amendText,
    setAmendText,
    resolveInFlight,
    displayedError,
    amendByteLength,
    amendIsTooLong,
    submitDisabled,
    primaryButtonsDisabled,
    handleUseMine,
    handleOpenAmend,
    handleCancelAmend,
    handleSubmitAmend,
  } = useInterpretationResolver({ event, sessionId, onResolved });

  const reactId = useId();
  const titleId = `${reactId}-title`;
  const amendInputId = `${reactId}-amend`;
  const errorId = `${reactId}-error`;
  const promptGateId = `${reactId}-prompt-gate`;
  const valueRegionId = `${reactId}-value`;

  const llmDraft = event.llm_draft ?? "";
  const userTerm = event.user_term ?? "this term";

  const requiresPromptView = event.kind === "llm_prompt_template";
  const valueIsLong =
    llmDraft.length > 140 || llmDraft.split("\n").length > 4;
  // invented_source shows pretty-printed JSON; short values inline, long
  // values behind the View expander.  The prompt template is ALWAYS behind
  // an expander — opened by the primary button's first stage.
  const hasViewExpander =
    requiresPromptView ||
    (event.kind === "invented_source" && valueIsLong);
  const hasInlineValue = event.kind === "invented_source" && !valueIsLong;

  const [expanded, setExpanded] = useState(false);
  // Two-control design for prompt cards (elspeth-3a4a65530f, audit
  // ux-review-2026-08-13): the small "View prompt" disclosure toggle is the
  // ONLY reveal, and the primary button is always "Approve" — disabled until
  // the prompt has been viewed.  This deliberately REVERSES the 2026-07-03
  // operator ask (one morphing button: click 1 reveals, click 2 approves),
  // because a control whose meaning changes under the pointer commits an
  // approval a double-clicking user never intended.  The old rationale —
  // a disabled Approve beside a View button "read as a dead end" — is
  // mitigated by the visible gate note naming the unlock condition.  Once
  // viewed, always viewed — collapsing the prompt afterwards does not
  // re-disable Approve.
  const [promptViewed, setPromptViewed] = useState(!requiresPromptView);

  const acceptDisabled = primaryButtonsDisabled;
  const approveGated = requiresPromptView && !promptViewed;

  // Focus on amend-mode toggle ONLY (never on mount — see file header).  Skip
  // the first run so mounting the card does not move focus.
  const amendTextareaRef = useRef<HTMLTextAreaElement | null>(null);
  const changeButtonRef = useRef<HTMLButtonElement | null>(null);
  const firstRunRef = useRef(true);
  useEffect(() => {
    if (firstRunRef.current) {
      firstRunRef.current = false;
      return;
    }
    if (mode === "amend") {
      amendTextareaRef.current?.focus();
    } else {
      changeButtonRef.current?.focus();
    }
  }, [mode]);

  const presentation = getCardPresentation(event, stepLabel);
  const chooseMode = mode === "choose" || !showAmend;

  // Resolved-prompt rendering (elspeth-990f5ea562): re-render the prompt
  // from the live composition state so accepted interpretation values appear
  // in place of the staging-time "pending interpretation" masks.  The frozen
  // event.llm_draft is demoted to a secondary "View original template"
  // disclosure, shown only when it differs from the resolved render.
  const promptDisplay = requiresPromptView
    ? resolvePromptDisplaySegments(compositionState, event)
    : null;
  const promptDisplayText =
    promptDisplay === null
      ? ""
      : promptDisplay.segments.map((segment) => segment.text).join("");
  const promptHasPendingSlot =
    promptDisplay !== null &&
    promptDisplay.segments.some((segment) => segment.kind === "pending");
  const showOriginalTemplate =
    promptDisplay !== null && llmDraft !== "" && promptDisplayText !== llmDraft;

  const promptHasResolvedSlot =
    promptDisplay !== null &&
    promptDisplay.segments.some((segment) => segment.kind === "resolved");

  const spinner = (
    <>
      <span className="ack-card-spinner" aria-hidden="true" />
      Saving…
    </>
  );

  const valueDisclosure = hasViewExpander ? (
    <div className="ack-card-value">
      {/* Sole disclosure control (elspeth-3a4a65530f): always rendered.
          For prompt cards the first open latches promptViewed so Approve
          unlocks; collapsing afterwards never re-locks it (once viewed,
          always viewed).  Exactly one control carries the name "View
          prompt" in every state — the duplicate-name trap the old
          morphing-primary design worked around is gone structurally. */}
      <Button
        variant="bare"
        // While Approve is gated, this disclosure IS the card's next action —
        // and it was the only control on the card NOT drawn as one (xs quiet
        // outline beside a big primary-styled-but-inert Approve, next to
        // sibling cards' filled active Acknowledge buttons). The --next
        // modifier gives it the filled active treatment until the first view
        // latches promptViewed; after that it recedes to the quiet toggle and
        // the unlocked Approve takes the emphasis back — one filled control
        // per card at a time (operator report 2026-08-16).
        className={
          approveGated
            ? "ack-card-view-toggle ack-card-view-toggle--next"
            : "ack-card-view-toggle"
        }
        aria-expanded={expanded}
        aria-controls={valueRegionId}
        onClick={() => {
          if (!expanded) setPromptViewed(true);
          setExpanded((prev) => !prev);
        }}
      >
        {requiresPromptView
          ? expanded
            ? "Hide prompt"
            : ACKNOWLEDGEMENT_VIEW_PROMPT_LABEL
          : expanded
            ? "Hide"
            : "View"}
      </Button>
      {expanded &&
        (requiresPromptView && promptDisplay !== null ? (
          <div
            id={valueRegionId}
            role="region"
            aria-label="Prompt template review"
            tabIndex={0}
            className="ack-card-prompt-template"
            style={PROMPT_TEMPLATE_STYLE}
          >
            {/* Visible key for the two slot tints (ux-review 2026-08-13).
                Both resolved and pending slots render as <mark> and differ
                only by CSS, so a sighted operator on an ATTESTATION surface
                had no way to tell an already-attested value from a sibling
                review's draft — while the sr-only per-slot prefixes below
                DID disambiguate, leaving AT users better served than sighted
                ones.  The distinction is carried by the TEXT, not the
                swatch: under forced-colors the swatch backgrounds are
                stripped and two identical squares would key nothing.  Each
                item renders only when a slot of that kind is present — the
                same one-way invariant as "never name a control the card does
                not render", applied to tints. */}
            {(promptHasResolvedSlot || promptHasPendingSlot) && (
              <ul className="ack-card-prompt-legend">
                {promptHasResolvedSlot && (
                  <li>
                    <span
                      className="ack-card-prompt-legend-swatch ack-card-prompt-slot--resolved"
                      aria-hidden="true"
                    />
                    Accepted value
                  </li>
                )}
                {promptHasPendingSlot && (
                  <li>
                    <span
                      className="ack-card-prompt-legend-swatch ack-card-prompt-slot--pending"
                      aria-hidden="true"
                    />
                    Pending value — awaiting its own review
                  </li>
                )}
              </ul>
            )}
            <pre className="ack-card-prompt-pre">
              {promptDisplay.segments.map((segment, index) =>
                segment.kind === "text" ? (
                  <span key={index}>{segment.text}</span>
                ) : (
                  <mark
                    key={index}
                    className={`ack-card-prompt-slot ack-card-prompt-slot--${segment.kind}`}
                  >
                    {/* The resolved/pending distinction is otherwise
                        CSS-only (background/border on <mark>), which is
                        invisible non-visually, so an SR user needs a
                        per-slot cue to audit which parts are still drafts
                        (WCAG 1.3.1).  Same two words the visible legend
                        uses, so the encodings agree. */}
                    <span className="visually-hidden">
                      {segment.kind === "pending"
                        ? "pending value: "
                        : "accepted value: "}
                    </span>
                    {segment.text}
                  </mark>
                ),
              )}
            </pre>
            {promptHasPendingSlot && (
              <p className="ack-card-prompt-pending-note">
                {/* Scoped to the PENDING tint only.  The previous wording
                    ("Highlighted values await their own interpretation
                    reviews") was false for every resolved slot, because
                    resolved slots are highlighted too — on a mixed prompt
                    it told the operator a value they had already attested
                    was still a draft. */}
                Pending values are drafts from their own reviews; the prompt
                runs with whatever you accept there.
              </p>
            )}
            {promptDisplay.usedFallback && (
              <p className="ack-card-prompt-pending-note">
                {/* Audit honesty (code-review 2026-08-13): the structured
                    parts could not be rendered, so this is the node's stored
                    template verbatim with no slots broken out.  A card whose
                    entire purpose is showing what actually runs must say so
                    rather than let the degraded render pass as the resolved
                    one. */}
                Showing the stored prompt template as-is — the interpretation
                slots could not be broken out for this card.
              </p>
            )}
            {showOriginalTemplate && (
              <details className="ack-card-original-template">
                <summary>View original template</summary>
                <pre className="ack-card-prompt-pre">{llmDraft}</pre>
              </details>
            )}
          </div>
        ) : (
          <div id={valueRegionId}>
            <CodeBlock
              code={llmDraft}
              prettyJson
              ariaLabel="Invented source data"
            />
          </div>
        ))}
    </div>
  ) : null;

  return (
    <section
      ref={sectionRef}
      id={acknowledgementCardDomId(event.id)}
      tabIndex={-1}
      className="ack-card"
      aria-labelledby={titleId}
      data-testid="acknowledgement-card"
    >
      <h3 id={titleId} className="ack-card-title">
        {presentation.title}
      </h3>

      <div className="ack-card-main">
        <p className="ack-card-line">{presentation.line}</p>
        {/* Gate note: says WHY Approve is disabled and names the unlock
            condition, so the gated primary is never an unexplained dead
            control (successor of the old scroll-gate note, elspeth-3b35abf148
            variant 2; kept visible and prominent as the mitigation for the
            pre-2026-07 "dead end" reading — see the promptViewed comment). */}
        {chooseMode && approveGated && (
          <p id={promptGateId} className="ack-card-gate-note">
            <strong>{ACKNOWLEDGEMENT_VIEW_PROMPT_LABEL}</strong> shows the
            LLM's instruction; {ACKNOWLEDGEMENT_APPROVE_LABEL} unlocks once
            you have viewed it.
          </p>
        )}
        {/* The disclosure precedes the gated primary in DOM AND tab order
            (ux-review 2026-08-13): "View prompt" is Approve's prerequisite,
            so reaching it second — and, in a 360px pane, finding it below the
            fold under a greyed-out primary — inverted the sequence.  Lives
            inside .ack-card-main on its own full-width flex row. */}
        {valueDisclosure}
        {chooseMode && (
          <div className="ack-card-actions">
            <Button
              ref={acceptButtonRef}
              variant="primary"
              className="ack-card-accept-btn"
              // Single-meaning control (elspeth-3a4a65530f): always the
              // decision-naming accept label; the disclosure semantics live
              // on the separate small View-prompt toggle.
              aria-label={presentation.acceptAriaLabel}
              aria-describedby={approveGated ? promptGateId : undefined}
              onClick={() => {
                // The VIEW GATE uses aria-disabled + a no-op click, the house
                // idiom for a gated primary carrying an attached reason
                // (ExecuteButton.tsx, elspeth-94c32de486): a natively
                // disabled button is skipped by SR navigation, which makes
                // the aria-describedby gate note — the whole mitigation for
                // the "dead end" reading — unreachable.  shared.css styles
                // .btn[aria-disabled="true"] identically to .btn:disabled,
                // so there is no visual change.  An in-flight resolve keeps
                // NATIVE disabled: that one must hard-block a double submit
                // and carries no explanatory note.
                if (approveGated) return;
                void handleUseMine();
              }}
              disabled={acceptDisabled}
              aria-disabled={approveGated ? true : undefined}
            >
              {resolveInFlight
                ? spinner
                : requiresPromptView
                  ? ACKNOWLEDGEMENT_APPROVE_LABEL
                  : ACKNOWLEDGEMENT_ACCEPT_LABEL}
            </Button>
            {showAmend && (
              <Button
                ref={changeButtonRef}
                className="ack-card-amend-btn"
                aria-label={`Change the interpretation of ${userTerm}`}
                onClick={handleOpenAmend}
                disabled={primaryButtonsDisabled}
              >
                {ACKNOWLEDGEMENT_AMEND_LABEL}
              </Button>
            )}
          </div>
        )}
      </div>

      {hasInlineValue && (
        <CodeBlock
          code={llmDraft}
          prettyJson
          ariaLabel="Invented source data"
        />
      )}

      {displayedError !== null && (
        <div id={errorId} role="alert" className="ack-card-error">
          <strong className="ack-card-error-heading">
            {displayedError.heading}
          </strong>
          <span className="ack-card-error-body">{displayedError.body}</span>
        </div>
      )}

      {!chooseMode && (
        <div className="ack-card-amend">
          <label htmlFor={amendInputId} className="ack-card-amend-label">
            What did you mean by <em>{userTerm}</em>?
          </label>
          <textarea
            ref={amendTextareaRef}
            id={amendInputId}
            className="ack-card-amend-input"
            value={amendText}
            onChange={(e) => setAmendText(e.target.value)}
            rows={4}
            disabled={resolveInFlight}
          />
          {amendIsTooLong && (
            <p className="ack-card-amend-cap-warning" role="status">
              Amendment is {amendByteLength} bytes; the maximum is{" "}
              {INTERPRETATION_AMENDMENT_MAX_BYTES} bytes.
            </p>
          )}
          <div className="ack-card-amend-actions">
            <Button
              className="ack-card-cancel-btn"
              onClick={handleCancelAmend}
              disabled={resolveInFlight}
            >
              Cancel
            </Button>
            <Button
              variant="primary"
              className="ack-card-submit-btn"
              onClick={() => void handleSubmitAmend()}
              disabled={submitDisabled}
            >
              {resolveInFlight ? spinner : "Submit"}
            </Button>
          </div>
        </div>
      )}
    </section>
  );
}
