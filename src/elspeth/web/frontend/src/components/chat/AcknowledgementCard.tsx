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
import type { InterpretationEvent, PromptReviewSnapshotResponse } from "@/types/interpretation";
import { getPromptReviewSnapshot } from "@/api/client";
import { Button } from "@/components/ui";
import { promptQueryCopyName, promptQueryEntries } from "@/lib/blobBindings";
import { PromptCopyButton } from "@/components/inspector/PromptCopyButton";
import { CodeBlock } from "./CodeBlock";
import {
  PENDING_INTERPRETATION_DISPLAY_TEXT,
  promptReviewDisplay,
  promptReviewIsUserSuppliedFiles,
  promptSlotDefinitionLabel,
  promptSlotLabel,
  promptSlotNotShownLabel,
  promptSlotViews,
  type PromptDisplayResult,
  type PromptSlotStatus,
  type PromptSlotView,
  type PromptSlotViews,
} from "./promptTemplateDisplay";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
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
import { preferredScrollBehavior } from "@/utils/motion";

const PROMPT_TEMPLATE_STYLE: CSSProperties = {
  maxHeight: "16rem",
  overflow: "auto",
};

/** The prompt card's stored review snapshot, kept with the display built from it. */
type PromptReviewState =
  | { status: "loading" }
  | { status: "ready"; snapshot: PromptReviewSnapshotResponse; display: PromptDisplayResult }
  | { status: "error"; reason: string };

const PROMPT_CHANGED_NOTICE =
  "This prompt changed since you opened it. The card now shows the current version; view it again before approving.";

/** Why Approve is unavailable when the snapshot request failed. */
/** The prompt region's content while there is no snapshot to approve. */
function placeholderDisplay(message: string): PromptDisplayResult {
  return { reviewAvailable: false, segments: [{ kind: "text", text: message }], text: message };
}

/** A slot's status as words: the visible suffix, the legend and the sr-only prefix agree. */
const SLOT_STATUS_WORDS: Record<PromptSlotStatus, string> = {
  pending: "pending",
  accepted: "accepted",
  unknown: "status not shown here",
};

const LEGEND_TEXT: Record<PromptSlotStatus, string> = {
  accepted: "Accepted — its own review is approved",
  pending: "Pending — awaiting its own review",
  unknown: "Status not shown here — see its own card",
};

/**
 * A legend entry. A slot whose approved definition cannot be shown has its
 * own entry: it has no card of its own, and the gate note gives the reason.
 * It shares the "unknown" swatch.
 */
type LegendKey = PromptSlotStatus | "not_shown";

const LEGEND_KEYS: readonly LegendKey[] = ["accepted", "pending", "unknown", "not_shown"];

const LEGEND_KEY_TEXT: Record<LegendKey, string> = {
  ...LEGEND_TEXT,
  not_shown: "Definition not shown: see the note above",
};

function slotViewLegendKey(view: PromptSlotView): LegendKey {
  return view.kind === "not_shown" ? "not_shown" : slotViewStatus(view);
}

/** The status a slot view is tinted, keyed and announced with. */
function slotViewStatus(view: PromptSlotView): PromptSlotStatus {
  if (view.kind === "definition") return "accepted";
  if (view.kind === "own_card") return view.status;
  return "unknown";
}

/** The visible text of a slot mark, in brackets as the delimiter. */
function slotViewLabel(view: PromptSlotView): string {
  if (view.kind === "definition") return `[${promptSlotDefinitionLabel(view.userTerm, view.choice, view.value)}]`;
  if (view.kind === "own_card") return `[${promptSlotLabel(view.userTerm)}: ${SLOT_STATUS_WORDS[view.status]}]`;
  return `[${promptSlotNotShownLabel(view.userTerm)}]`;
}

const NO_SLOT_VIEWS: PromptSlotViews = { slots: [], failure: null };

function snapshotLoadReason(err: unknown): string {
  const code = typeof err === "object" && err !== null && "error_type" in err ? (err as { error_type?: unknown }).error_type : undefined;
  if (code === "prompt_review_snapshot_not_found") {
    return "The reviewed prompt could not be loaded: this review has no stored text, so it cannot be approved. Ask the composer to surface it again.";
  }
  return "The reviewed prompt could not be loaded, so it cannot be approved. Reload the session to try again.";
}

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
  element.scrollIntoView({ behavior: preferredScrollBehavior(), block: "center" });
  element.focus({ preventScroll: true });
}

function assertNever(value: never): never {
  throw new Error(`Unhandled interpretation kind: ${String(value)}`);
}

/**
 * Parsed shape of the server-computed source_data_contract draft JSON
 * (src/elspeth/web/composer/source_demand.py::build_source_data_contract_draft).
 * The draft carries the FACTS — demanded fields, illustrative sample header,
 * per-field sample misses — and this card carries the prose.
 */
interface DataContractDraft {
  demandedFields: string[];
  sampleHeader: string[] | null;
  missingFromSample: string[];
}

const DATA_CONTRACT_DRAFT_VERSION = 2;
const DATA_CONTRACT_DRAFT_KEYS = [
  "contract_version",
  "kind",
  "demanded_fields",
  "sample_header",
  "missing_from_sample",
] as const;

function compareUnicodeCodePoints(left: string, right: string): number {
  const leftPoints = Array.from(left, (char) => char.codePointAt(0) ?? 0);
  const rightPoints = Array.from(right, (char) => char.codePointAt(0) ?? 0);
  const sharedLength = Math.min(leftPoints.length, rightPoints.length);
  for (let index = 0; index < sharedLength; index += 1) {
    const difference = leftPoints[index] - rightPoints[index];
    if (difference !== 0) return difference;
  }
  return leftPoints.length - rightPoints.length;
}

function stringArraysEqual(left: string[], right: string[]): boolean {
  return (
    left.length === right.length &&
    left.every((value, index) => value === right[index])
  );
}

export function parseDataContractDraft(draft: string): DataContractDraft | null {
  let payload: unknown;
  try {
    payload = JSON.parse(draft);
  } catch {
    return null;
  }
  if (typeof payload !== "object" || payload === null) return null;
  const record = payload as Record<string, unknown>;
  const keys = Object.keys(record);
  if (
    keys.length !== DATA_CONTRACT_DRAFT_KEYS.length ||
    !DATA_CONTRACT_DRAFT_KEYS.every((key) => keys.includes(key)) ||
    record["contract_version"] !== DATA_CONTRACT_DRAFT_VERSION ||
    record["kind"] !== "source_data_contract"
  ) {
    return null;
  }
  const demanded = record["demanded_fields"];
  if (
    !Array.isArray(demanded) ||
    !demanded.every((field) => typeof field === "string")
  ) {
    return null;
  }
  const demandedFields = demanded as string[];
  const canonicalDemanded = [...new Set(demandedFields)].sort(
    compareUnicodeCodePoints,
  );
  if (
    demandedFields.length === 0 ||
    !stringArraysEqual(demandedFields, canonicalDemanded)
  ) {
    return null;
  }
  const sample = record["sample_header"];
  if (
    sample !== null &&
    (!Array.isArray(sample) ||
      !sample.every((cell) => typeof cell === "string"))
  ) {
    return null;
  }
  const missing = record["missing_from_sample"];
  if (
    !Array.isArray(missing) ||
    !missing.every((field) => typeof field === "string")
  ) {
    return null;
  }
  const missingFromSample = missing as string[];
  const sampleHeader = sample as string[] | null;
  const expectedMissing =
    sampleHeader === null
      ? []
      : demandedFields.filter((field) => !sampleHeader.includes(field));
  if (!stringArraysEqual(missingFromSample, expectedMissing)) return null;
  return {
    demandedFields,
    sampleHeader,
    missingFromSample,
  };
}

interface CardPresentation {
  /** Title row: humanised step label · kind (e.g. "Summarise step · model"). */
  title: string;
  /** One punchy, LLM-attributed line. */
  line: ReactNode;
  /** Accessible label for the Acknowledge button (names the decision). */
  acceptAriaLabel: string;
}

/**
 * Strip a trailing `[user_term: ...]` annotation from a pipeline_decision
 * draft body. The annotation is registry plumbing that older backend draft
 * constants embedded verbatim (removed at the source by elspeth-9665dcca32,
 * but events persisted before that fix still carry it), and the card is a
 * user surface — internal registry keys never render.
 */
function stripUserTermAnnotation(draft: string): string {
  return draft.replace(/\s*\[user_term:[^\]]*\]\s*$/, "");
}

/**
 * The card-title fragment for a caller that holds only a step LABEL. Keeps
 * the pre-elspeth-... wording exactly; production callers pass a resolved
 * `stepTitle` (humaniseStepTitle), which additionally disambiguates a removed
 * step by its ghost id.
 */
function defaultStepTitle(stepLabel: string): string {
  return `${stepLabel} step`;
}

/**
 * `stepLabel` names the step MID-SENTENCE ("…columns from Summarise"), while
 * `stepTitle` is the card-title fragment ("Summarise step", "Removed step
 * (was Extract Invoice)"). Two registers of one name: the title slot has room
 * to disambiguate a deleted step, the prose slot reads worse if it tries.
 */
function getCardPresentation(
  event: InterpretationEvent,
  stepLabel: string,
  stepTitle: string,
): CardPresentation {
  const userTerm = event.user_term ?? "this term";
  const llmDraft = event.llm_draft ?? "";
  switch (event.kind) {
    case "llm_prompt_template":
      return {
        title: `${stepTitle} · prompt`,
        line: "The LLM wrote the instruction for this step.",
        // Prompt cards use the two-stage View→Approve button, so the accept
        // action is named "Approve" (visible label and accessible name must
        // agree — WCAG 2.5.3 label-in-name).
        acceptAriaLabel: "Approve the LLM prompt template",
      };
    case "pipeline_decision":
      return {
        title: `${stepTitle} · decision`,
        line: (
          <span className="ack-card-decision">
            {stripUserTermAnnotation(llmDraft) || "(no decision recorded)"}
          </span>
        ),
        acceptAriaLabel: "Acknowledge the pipeline decision",
      };
    case "llm_model_choice":
      return {
        title: `${stepTitle} · model`,
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
    case "source_data_contract":
      return {
        title: "Data contract",
        line: (
          <>
            This pipeline relies on certain columns from{" "}
            {stepLabel ? <em>{stepLabel}</em> : "this source"}. Acknowledging
            promises that <strong>whatever you feed this pipeline</strong> will
            carry these columns — not just the file you uploaded.
          </>
        ),
        acceptAriaLabel: "Acknowledge the source data contract",
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
  stepTitle: string = defaultStepTitle(stepLabel),
): string {
  return getCardPresentation(event, stepLabel, stepTitle).title;
}

export interface AcknowledgementCardProps {
  /** The pending interpretation event to acknowledge. */
  event: InterpretationEvent;
  /** Owning session id; round-tripped to the store actions. */
  sessionId: string;
  /** Humanised step label resolved from the composition (e.g. "Summarise"),
   *  used where the step is named mid-sentence. */
  stepLabel: string;
  /** The card-title fragment resolved from the composition
   *  (`humaniseStepTitle`, e.g. "Summarise step" or "Removed step (was
   *  Extract Invoice)"). Optional: a caller holding only a label gets
   *  `${stepLabel} step`, which is what every caller rendered before a
   *  removed step needed disambiguating. */
  stepTitle?: string;
  /** Render the inline amend affordance when the parent allows amendments. */
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
  stepTitle,
  showAmend = false,
  onResolved,
  acceptButtonRef,
  sectionRef,
}: AcknowledgementCardProps) {
  const requiresPromptView = event.kind === "llm_prompt_template";
  // A prompt card renders ONLY from the server's stored review snapshot, and
  // Approve sends THAT snapshot's anchor. Snapshot and display live in one
  // state object, so what is shown and what is approved cannot diverge; the
  // browser's compositionState is deliberately not an input (a second tab or
  // a failed state read can leave it behind the server).
  const [promptReview, setPromptReview] = useState<PromptReviewState>({ status: "loading" });
  const [promptFetchSeq, setPromptFetchSeq] = useState(0);
  const [promptChangedNotice, setPromptChangedNotice] = useState<string | null>(null);
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
  // re-disable Approve.  A changed snapshot resets it: the new text has not
  // been viewed.
  const [promptViewed, setPromptViewed] = useState(!requiresPromptView);
  const [expanded, setExpanded] = useState(false);
  useEffect(() => {
    if (!requiresPromptView) return;
    const controller = new AbortController();
    setPromptReview({ status: "loading" });
    getPromptReviewSnapshot(sessionId, event.id, controller.signal)
      .then((snapshot) => {
        if (!controller.signal.aborted) {
          setPromptReview({ status: "ready", snapshot, display: promptReviewDisplay(event.id, snapshot) });
        }
      })
      .catch((err: unknown) => {
        if (!controller.signal.aborted) setPromptReview({ status: "error", reason: snapshotLoadReason(err) });
      });
    return () => controller.abort();
  }, [requiresPromptView, sessionId, event.id, promptFetchSeq]);

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
  } = useInterpretationResolver({
    event,
    sessionId,
    onResolved,
    reviewedAnchor: promptReview.status === "ready" ? promptReview.snapshot.anchor : null,
    onPromptReviewChanged: () => {
      // Never auto-approve the replacement: re-fetch, re-gate on viewing it,
      // and say why in the live region.
      setPromptChangedNotice(PROMPT_CHANGED_NOTICE);
      setPromptViewed(false);
      setExpanded(false);
      setPromptFetchSeq((seq) => seq + 1);
    },
  });

  const reactId = useId();
  const titleId = `${reactId}-title`;
  const amendInputId = `${reactId}-amend`;
  const errorId = `${reactId}-error`;
  const promptGateId = `${reactId}-prompt-gate`;
  const valueRegionId = `${reactId}-value`;

  const llmDraft = event.llm_draft ?? "";
  const userTerm = event.user_term ?? "this term";

  // Wording only. Who wrote the instruction is said only once the stored
  // snapshot says it: files a user supplied, or (every other loaded case)
  // the LLM. While the snapshot is loading, or when it cannot be loaded,
  // the card names no author, so a card for a user's file never shows
  // "The LLM wrote the instruction", even for a moment. The view gate and
  // what Approve sends do not read any of this.
  const promptIsUserSuppliedFiles = promptReview.status === "ready" && promptReviewIsUserSuppliedFiles(promptReview.snapshot);

  const promptDisplay: PromptDisplayResult | null = !requiresPromptView
    ? null
    : promptReview.status === "ready"
      ? promptReview.display
      : placeholderDisplay(
          promptReview.status === "loading" ? "Loading the reviewed prompt…" : "The reviewed prompt is unavailable.",
        );
  // Each slot from the snapshot's frozen skeleton: a slot naming an event
  // shows that event's approved definition (checked against the snapshot's
  // rendered prompt); a slot with no event shows its own review's live
  // status and no definition. A slot that cannot be shown leaves the card
  // with no Approve (`slotViews.failure` says why).
  const pendingEvents = useInterpretationEventsStore((s) => s.pendingBySession[sessionId]);
  const resolvedEvents = useInterpretationEventsStore((s) => s.resolvedBySession[sessionId]);
  const slotViews: PromptSlotViews =
    promptReview.status === "ready"
      ? promptSlotViews(promptReview.display, promptReview.snapshot, event.affected_node_id, pendingEvents, resolvedEvents)
      : NO_SLOT_VIEWS;
  const legendKeys = new Set(slotViews.slots.map(slotViewLegendKey));
  const promptHasSlot = slotViews.slots.length > 0;
  const promptHasDefinition = slotViews.slots.some((view) => view.kind === "definition");
  const promptHasOwnCardSlot = slotViews.slots.some((view) => view.kind === "own_card");
  const promptSlotsAllOwnCard = promptHasSlot && slotViews.slots.every((view) => view.kind === "own_card");
  const promptReviewAvailable = promptDisplay?.reviewAvailable === true && slotViews.failure === null;
  const valueIsLong =
    llmDraft.length > 140 || llmDraft.split("\n").length > 4;
  // invented_source shows pretty-printed JSON; short values inline, long
  // values behind the View expander.  The prompt template is ALWAYS behind
  // an expander — opened by the primary button's first stage.
  const hasViewExpander =
    requiresPromptView ||
    (event.kind === "invented_source" && valueIsLong);
  const hasInlineValue = event.kind === "invented_source" && !valueIsLong;

  const acceptDisabled = primaryButtonsDisabled;
  // An open disclosure shows the complete prompt as soon as loading finishes.
  // A closed preview never counts as viewing the subsequently loaded content.
  const approveGated = requiresPromptView && (!promptReviewAvailable || (!promptViewed && !expanded));

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

  const presentation = getCardPresentation(
    event,
    stepLabel,
    stepTitle ?? defaultStepTitle(stepLabel),
  );
  const cardLine = !requiresPromptView
    ? presentation.line
    : promptReview.status !== "ready"
      ? "This step's instruction is waiting for your approval."
      : promptIsUserSuppliedFiles
        ? "The instruction for this step comes from a file supplied by a user."
        : presentation.line;
  const chooseMode = mode === "choose" || !showAmend;

  // Data-contract body: the demanded columns with per-field sample warnings,
  // the illustrative sample note, and the honest consequence of acknowledging
  // (a valid source row that breaks the producer guarantee stops the run).
  // Source-validation quarantine is a separate, earlier path. Falls back
  // to the raw draft when the payload cannot be parsed — an attestation
  // surface must never silently render a degraded summary as the real one.
  const dataContract =
    event.kind === "source_data_contract"
      ? parseDataContractDraft(llmDraft)
      : null;
  const dataContractBody =
    event.kind === "source_data_contract" ? (
      dataContract !== null ? (
        <div className="ack-card-data-contract">
          <ul className="ack-card-data-contract-fields">
            {dataContract.demandedFields.map((field) => {
              const missingFromSample =
                dataContract.missingFromSample.includes(field);
              return (
                <li key={field}>
                  <code>{field}</code>
                  {missingFromSample && (
                    <span
                      className="ack-card-data-contract-warning"
                      role="alert"
                    >
                      {" "}
                      — your data doesn't appear to have this. Fix the data or
                      change the pipeline.
                    </span>
                  )}
                </li>
              );
            })}
          </ul>
          <p className="ack-card-data-contract-note">
            {dataContract.sampleHeader !== null ? (
              <>
                Sample columns seen in your file:{" "}
                {dataContract.sampleHeader.join(", ")} (illustrative only — the
                promise covers every row you feed this pipeline, not this
                sample).
              </>
            ) : (
              <>
                No sample of this source's data was available; the promise
                covers every row you feed this pipeline.
              </>
            )}
          </p>
          <p className="ack-card-data-contract-note">
            Each valid row must carry the column in both its data and emitted
            schema contract; the value may be empty. If either omits one of
            these columns, the run stops and records a source data-contract
            failure. Rows quarantined during source validation are handled
            separately and never reach this check.
          </p>
        </div>
      ) : (
        <div className="ack-card-data-contract">
          <p className="ack-card-data-contract-note">
            The data-contract details could not be parsed; showing the stored
            card payload as-is.
          </p>
          <CodeBlock code={llmDraft} prettyJson ariaLabel="Data contract" />
        </div>
      )
    ) : null;

  // The event's bounded llm_draft stays reachable behind a secondary "View
  // original template" disclosure, shown only when it differs from the
  // snapshot render.
  const reviewedDisplayText = promptDisplay?.systemPrompt !== undefined && promptDisplay.systemPrompt !== null
    ? `System prompt:\n${promptDisplay.systemPrompt}\n\nPrompt template:\n${promptDisplay.text}`
    : (promptDisplay?.text ?? "");
  const showOriginalTemplate =
    promptReview.status === "ready" && promptDisplay !== null && llmDraft !== "" && reviewedDisplayText !== llmDraft;
  const promptGateReason =
    promptReview.status === "error"
      ? promptReview.reason
      : slotViews.failure !== null
        ? slotViews.failure
        : "The complete prompt is not available yet. Reload the session if it does not load; the preview cannot be approved.";

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
          if (promptReviewAvailable) setPromptViewed(true);
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
            {/* Visible key for the slot tints (ux-review 2026-08-13), now
                keyed to each slot's review STATUS. The distinction is carried
                by the TEXT, not the swatch: under forced colors the swatch
                backgrounds are stripped. Each item renders only when a slot
                in that state is present. */}
            {promptHasSlot && (
              <ul className="ack-card-prompt-legend">
                {LEGEND_KEYS.filter((key) => legendKeys.has(key)).map((key) => (
                  <li key={key}>
                    <span
                      className={`ack-card-prompt-legend-swatch ack-card-prompt-slot--${key === "not_shown" ? "unknown" : key}`}
                      aria-hidden="true"
                    />
                    {LEGEND_KEY_TEXT[key]}
                  </li>
                ))}
              </ul>
            )}
            {/* Each prompt's heading, then the file it is read from (display
                only: Approve sends the snapshot's anchor, never this text). */}
            {promptDisplay.systemPrompt !== undefined && (
              <>
                {promptDisplay.systemPrompt !== null && (
                  <>
                    <p className="ack-card-prompt-role">System prompt</p>
                    {promptDisplay.systemPromptFile !== undefined && (
                      <p className="ack-card-prompt-file">File: {promptDisplay.systemPromptFile}</p>
                    )}
                    <pre className="ack-card-prompt-pre">{promptDisplay.systemPrompt}</pre>
                  </>
                )}
                <p className="ack-card-prompt-role">User prompt</p>
                {promptDisplay.promptTemplateFile !== undefined && (
                  <p className="ack-card-prompt-file">File: {promptDisplay.promptTemplateFile}</p>
                )}
              </>
            )}
            <pre className="ack-card-prompt-pre">
              {(() => {
                let slotIndex = 0;
                return promptDisplay.segments.map((segment, index) => {
                  if (segment.kind === "text") return <span key={index}>{segment.text}</span>;
                  const view = slotViews.slots[slotIndex++];
                  const status = slotViewStatus(view);
                  return (
                    <mark key={index} className={`ack-card-prompt-slot ack-card-prompt-slot--${status}`}>
                      {/* The tint is CSS-only, so the status is also in the
                          span's text (forced colors strip the tint) and in
                          an sr-only prefix (WCAG 1.3.1). Same words as the
                          legend, so the encodings agree. A definition is
                          shown whole, as text, after the term's name. */}
                      <span className="visually-hidden">{`interpretation slot, ${SLOT_STATUS_WORDS[status]}: `}</span>
                      {slotViewLabel(view)}
                    </mark>
                  );
                });
              })()}
            </pre>
            {promptReview.status === "ready" && (() => {
              const surface = promptReview.snapshot.authored_surface;
              const queries = promptQueryEntries(surface.queries).filter((query) => typeof query.entry.template === "string");
              if (typeof surface.system_prompt !== "string" && typeof surface.prompt_template !== "string" && queries.length === 0) return null;
              // One wrapping row under the text, so several copy controls
              // line up instead of running on as inline buttons.
              return <div className="ack-card-copy-actions">
                {typeof surface.system_prompt === "string" && (
                  <PromptCopyButton value={surface.system_prompt} label="system prompt" showLabel />
                )}
                {typeof surface.prompt_template === "string" && (
                  <PromptCopyButton value={surface.prompt_template} label="user prompt template" showLabel />
                )}
                {queries.map((query) => (
                  <PromptCopyButton key={query.selector} value={String(query.entry.template)} label={`prompt template for query ${promptQueryCopyName(query)}`} showLabel />
                ))}
              </div>;
            })()}
            {promptHasDefinition && (
              <p className="ack-card-prompt-pending-note">
                Terms shown with a definition keep their approved definitions,
                shown in place; this card approves the wording around them.
              </p>
            )}
            {promptHasOwnCardSlot && (
              <p className="ack-card-prompt-pending-note">
                {promptSlotsAllOwnCard
                  ? "A marked term is reviewed in its own card, which approves its value; this card approves the prompt around it."
                  : "A term marked “reviewed in its own card” has its value approved in that card."}
              </p>
            )}
            {promptDisplay.hasPendingSlot === true && (
              <p className="ack-card-prompt-pending-note">
                “{PENDING_INTERPRETATION_DISPLAY_TEXT}” marks a term you review
                in its own card; the prompt runs with the value you accept there.
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
      data-affected-node-id={event.affected_node_id ?? undefined}
    >
      <h3 id={titleId} className="ack-card-title">
        {presentation.title}
      </h3>

      <div className="ack-card-main">
        <p className="ack-card-line">{cardLine}</p>
        {/* Always mounted for a prompt card so a changed-snapshot notice is
            announced when it appears (a live region inserted with its text
            already inside is not reliably read). The replacement text is
            never swapped in silently, and never approved for the user. */}
        {requiresPromptView && (
          <p role="status" className="ack-card-review-notice">{promptChangedNotice}</p>
        )}
        {dataContractBody}
        {/* Gate note: says WHY Approve is disabled and names the unlock
            condition, so the gated primary is never an unexplained dead
            control (successor of the old scroll-gate note, elspeth-3b35abf148
            variant 2; kept visible and prominent as the mitigation for the
            pre-2026-07 "dead end" reading — see the promptViewed comment). */}
        {chooseMode && approveGated && (
          <p id={promptGateId} className="ack-card-gate-note">
            {!promptReviewAvailable ? (
              promptGateReason
            ) : (
              <>
            <strong>{ACKNOWLEDGEMENT_VIEW_PROMPT_LABEL}</strong> shows the{" "}
            {promptIsUserSuppliedFiles ? "file's text" : "LLM's instruction"};{" "}
            {ACKNOWLEDGEMENT_APPROVE_LABEL} unlocks once you have viewed it.
              </>
            )}
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
            // Characters, not bytes, in the sentence the writer reads. The
            // byte overage is an UPPER bound on the characters to remove
            // (multibyte text shortens faster), so "about" is honest.
            //
            // The exact figures go in an .sr-only span, NOT in `title` alone.
            // This <p> is not focusable, so a keyboard user gets no hover and
            // no focus tooltip; and `title` on a role="status" element is a
            // naming fallback, not part of the live-region announcement, so a
            // screen-reader user would hear only the approximate count. The
            // .sr-only span is inside the live region and is announced with it.
            <p className="ack-card-amend-cap-warning" role="status">
              Shorten this by about{" "}
              {amendByteLength - INTERPRETATION_AMENDMENT_MAX_BYTES} characters
              to fit the {INTERPRETATION_AMENDMENT_MAX_BYTES / 1024} KB limit.
              <span className="sr-only">
                {" "}({amendByteLength} bytes; the maximum is{" "}
                {INTERPRETATION_AMENDMENT_MAX_BYTES} bytes.)
              </span>
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
