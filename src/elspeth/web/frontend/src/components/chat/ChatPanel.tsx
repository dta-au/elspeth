// src/components/chat/ChatPanel.tsx
import {
  Fragment,
  useEffect,
  useMemo,
  useRef,
  useCallback,
  useState,
  type SetStateAction,
} from "react";
import { Button } from "@/components/ui";
import {
  createInterpretationResolutionHandler,
  useSessionStore,
} from "@/stores/sessionStore";
import {
  selectApprovedInterpretations,
  useInterpretationEventsStore,
} from "@/stores/interpretationEventsStore";
import type { InterpretationEvent } from "@/types/interpretation";
import type { ValidationEntryDTO } from "@/types/index";
import {
  projectInlineSourceSummary,
  useInlineSourceStore,
} from "@/stores/inlineSourceStore";
import { useComposer } from "@/hooks/useComposer";
import {
  getBlobMetadata,
  previewBlobContent,
  toInlineSourceProvenance,
} from "@/api/client";
import { MessageBubble } from "./MessageBubble";
import { groupIntoTurns, turnRepresentativeMessage, type ChatTurn } from "./turns";
import { ComposingIndicator } from "./ComposingIndicator";
import { AuthorityChip } from "./AuthorityChip";
import {
  ChatInput,
  uploadedBlobPromptSentence,
} from "./ChatInput";
import { FreeformIntroduction } from "./FreeformIntroduction";
import { BlobManager } from "@/components/blobs/BlobManager";
import { actionableProposals } from "./actionableProposals";
import { useCompletionOutcome } from "./completionOutcome";
import { makePhraseFor } from "@/lib/validationHumaniser";
import {
  AcknowledgementStack,
  usePendingAcknowledgements,
} from "./AcknowledgementStack";
import { stepLabelForNodeId } from "./interpretationStepLabel";
import { ApprovalReadinessRow } from "@/components/workflow/ApprovalReadinessRow";
import { DecisionPanel, DecisionPanelLiveRegion } from "./DecisionPanel";
import { projectDecisionRows } from "./decisionPanelRows";
import { useExecutionStore } from "@/stores/executionStore";
import { applySuggestionPrompt, askAboutBlockerDraft, repairGraphPrompt } from "@/lib/suggestionPrompts";
import { dispatchArtifactViewIntent } from "@/lib/composer-events";
import {
  COMPOSE_CONNECTING_MESSAGE,
  COMPOSE_UNAVAILABLE_MESSAGE,
} from "@/config/composer";
import { InlineSourceCreatedTurn } from "./InlineSourceCreatedTurn";
import { InlineSourceFallbackPrompt } from "./InlineSourceFallbackPrompt";
import { sortedSourceEntries } from "@/utils/compositionState";
import { preferredScrollBehavior } from "@/utils/motion";
import type {
  BlobMetadata,
  ChatMessage,
  CompositionState,
  InlineSourceSummary,
} from "@/types/api";

function isTerminalComposerPhase(
  phase: string | null | undefined,
): boolean {
  return phase === "complete" || phase === "failed" || phase === "cancelled";
}

// Stable empty slice for the resolved-interpretations selector. A selector
// that returns a fresh `[]` on every call compares unequal to itself under
// zustand's referential check and re-renders ChatPanel on every store write,
// so the miss path has to hand back ONE array.
const NO_RESOLVED_INTERPRETATIONS: readonly InterpretationEvent[] = [];

/**
 * "Got it — using your interpretation of <term>." — the human-readable echo
 * of an interpretation the operator approved.
 *
 * The canonical record is the interpretation_event row in the audit trail;
 * this is the nudge that tells the operator, in the conversation, which
 * assumption they just signed off when no graph approval table is available.
 * One component because it now renders from
 * TWO places — anchored beside the turn that raised the term, and after the
 * stream for rows that carry no usable anchor — and the two must stay the
 * same bubble.
 *
 * The node clause is what keeps two approvals of the SAME term apart
 * (elspeth-52be5924d7): the staging dedup is per (kind, user_term,
 * affected_node_id), and pipeline_decision terms are fixed constants
 * ('drop_raw_html_fields' on every raw-HTML cleanup), so one turn can
 * legitimately raise the same term against two nodes. Without the node the
 * two echoes are pixel-identical and the operator cannot tell which
 * approval bound to which review.
 *
 * REGISTER (elspeth-3574f87208, operator ruling 2026-09-01): this is a
 * SYSTEM attestation, not assistant speech — the assistant never uttered
 * it; the backend wrote it on the operator's own action. It therefore must
 * NOT compose `message-row--assistant` + `bubble-assistant` (which assert
 * an assistant reply, and made unanchorable confirmations at the tail read
 * as replies to the newest message). It renders in its own approval
 * register instead — the same third-register move as `.bubble-system` /
 * `.trusted-system-notice`. The `resolved_at` timestamp is temporal
 * honesty: the POSITION of an anchored echo is causal attribution (which
 * turn raised the term), while the operator may have approved it many
 * turns later — the visible time keeps an old-turn-anchored echo from
 * implying the decision happened back then.
 */
const APPROVAL_TIME_FORMATTER = new Intl.DateTimeFormat(undefined, {
  month: "short",
  day: "numeric",
  hour: "numeric",
  minute: "2-digit",
});

function InterpretationConfirmation({
  userTerm,
  kind,
  affectedNodeId,
  resolvedAt,
}: {
  userTerm: string;
  kind: InterpretationEvent["kind"];
  affectedNodeId: string | null;
  resolvedAt: string | null;
}) {
  // Validate BEFORE formatting: Intl.DateTimeFormat.format() THROWS
  // RangeError on an invalid Date, so an unparseable resolved_at must drop
  // the timestamp rather than crash the transcript.
  const resolvedStamp = resolvedAt === null ? null : new Date(resolvedAt);
  const resolvedLabel =
    resolvedStamp !== null && !Number.isNaN(resolvedStamp.getTime())
      ? APPROVAL_TIME_FORMATTER.format(resolvedStamp)
      : null;
  const approvalLabel = kind === null || kind === "vague_term" ? null : {
    invented_source: "Approved the generated data",
    llm_prompt_template: "Approved the prompts",
    pipeline_decision: "Approved the pipeline decision",
    llm_model_choice: "Approved the model selection",
    source_data_contract: "Approved the input data requirements",
  }[kind];
  return (
    <div
      className="message-row message-row--interpretation-approval interpretation-review-confirmation"
      data-testid="interpretation-review-confirmation"
      role="status"
    >
      <div className="interpretation-approval-note">
        <span className="interpretation-approval-check" aria-hidden="true">
          ✓
        </span>{" "}
        {approvalLabel ?? <>
          Got it — using your interpretation of{" "}
          <em className="interpretation-review-confirmation-user-term">
            {userTerm}
          </em>
        </>}
        {affectedNodeId !== null && (
          <>
            {" "}
            for{" "}
            <em className="interpretation-review-confirmation-node">
              {affectedNodeId}
            </em>
          </>
        )}
        .
        {resolvedAt !== null && resolvedLabel !== null && (
          <>
            {" "}
            <time
              className="interpretation-approval-time"
              dateTime={resolvedAt}
            >
              {resolvedLabel}
            </time>
          </>
        )}
      </div>
    </div>
  );
}





// ── Inline-source fallback heuristic (Phase 5a Task 5) ───────────────────────
//
// `looksLikeData` is the safety-net predicate the chat panel runs against
// recent user messages to decide whether to surface the
// InlineSourceFallbackPrompt. The widget is the floor for the
// inline-source-from-chat path: if the composer LLM ignores Task 8's
// prompt nudge and never proposes a `set_pipeline` with an inline_blob
// source for source-shaped typed input, this predicate triggers the
// fallback affordance so the user is not stuck.
//
// CLOSED LIST — two recognised shapes. Both are HIGH-SPECIFICITY signals.
// The Phase 5a Task 5 spec also lists a third clause ("single short typed
// phrase under 200 chars containing no ?"), but that clause matches almost
// every chat message the user could type and would dominate the predicate
// — biasing toward FALSE POSITIVES (a disruptive affordance surfacing on
// every conversational turn), which is the opposite of the spec's
// "bias toward false negatives" framing. Surfacing the fallback on a
// missed URL is recoverable (the user re-types or accepts the fallback);
// surfacing it on every casual message is a UX bug. We deliberately omit
// clause 3; cf. the InlineSourceFallbackPrompt self-review in the
// commit message.
//
//   1. URL — http(s) prefix anywhere in the content.
//   2. Comma-separated list — 2..10 comma-separated tokens (matches a
//      typed list like "alice, bob, carol" but not a normal English
//      sentence with one or two embedded commas because we require the
//      entire trimmed content to consist of comma-separated tokens).
//
/** Min items in a typed list to qualify (2 items = at least one comma). */
const LIST_TOKEN_MIN_COUNT = 2;
/** Max items in a typed list to qualify (spec §Task 5 detection §3). */
const LIST_TOKEN_MAX_COUNT = 10;
/**
 * Per-token max word count.  A typed-source list token is almost always
 * 1..3 words — names ("Alice Smith"), slugs ("government-data"), URLs
 * ("a.com"), or short identifiers.  English prose with an embedded
 * comma ("hello, world how are you doing today") has multi-word tokens
 * (6+ words after the first split).
 *
 * Bias toward false negatives: a list of multi-word phrases longer
 * than 3 words (e.g. "5 government web pages, the local council site,
 * the open-data portal") would fail this check.  That's deliberate
 * — the predicate is the SAFETY NET; missing a phrase-shaped list is
 * recoverable (user re-types more concisely, or the LLM proposes a
 * source on the next turn).  Over-firing on every English sentence
 * with one comma is the worse failure mode.
 */
const LIST_TOKEN_MAX_WORDS = 3;

// Exported for the ChatPanel test seam — unit-tested directly so the
// predicate's shape is pinned without going through the widget render.
export function looksLikeData(content: string): boolean {
  const trimmed = content.trim();
  if (trimmed === "") return false;
  if (/https?:\/\//.test(trimmed)) return true;
  // Comma-separated list of 2..10 short tokens.  We split (not regex-
  // match) so we can apply the per-token word-count cap structurally.
  // The earlier regex-only `^[^,]+(?:, [^,]+){1,9}$` approach matched
  // any prose containing a comma because `[^,]+` is greedy on
  // whitespace; a structural check is clearer and easier to evolve.
  const parts = trimmed.split(",").map((p) => p.trim());
  if (parts.length < LIST_TOKEN_MIN_COUNT) return false;
  if (parts.length > LIST_TOKEN_MAX_COUNT) return false;
  // Every token must be non-empty (rejects trailing-comma artefacts
  // like "a, b,") AND ≤ LIST_TOKEN_MAX_WORDS words (rejects prose
  // with one or two embedded commas).
  for (const part of parts) {
    if (part === "") return false;
    // Split on any whitespace run; filter empties from the leading
    // or trailing edge already handled by trim, but defensive against
    // double-spaces.
    const words = part.split(/\s+/).filter((w) => w.length > 0);
    if (words.length > LIST_TOKEN_MAX_WORDS) return false;
  }
  return true;
}

/** Narrow `source.options["blob_ref"]` (which is `unknown`) to a string. */
function readSourceBlobRef(source: { options: Record<string, unknown> } | null): string | null {
  if (source === null) return null;
  const raw = source.options["blob_ref"];
  return typeof raw === "string" && raw !== "" ? raw : null;
}

function readBlobRefs(state: CompositionState | null): string[] {
  if (state === null) return [];
  return [...new Set(sortedSourceEntries(state).flatMap(([, source]) => {
    const ref = readSourceBlobRef(source);
    return ref === null ? [] : [ref];
  }))];
}

function isInlineSourceBlob(metadata: BlobMetadata): boolean {
  return (
    metadata.created_by === "assistant" &&
    metadata.created_from_message_id !== null
  );
}




interface ChatPanelProps {
  onOpenSecrets?: () => void;
  /** Embedded flows may need to keep their current session binding. */
  allowFork?: boolean;
}








/**
 * Main chat panel combining the message list, composing indicator, and input.
 *
 * Auto-scrolls to the bottom on new messages unless the user has scrolled up.
 * Focus returns to the ChatInput textarea after the assistant response arrives.
 */
export function ChatPanel(props: ChatPanelProps) {
  const composer = useComposer();
  return <ChatPanelContent {...props} composer={composer} />;
}

/** Chat controls sharing the caller's compose and cancellation owner. */
export function ChatPanelContent({
  onOpenSecrets,
  allowFork = true,
  composer,
}: ChatPanelProps & { composer: ReturnType<typeof useComposer> }) {
  const messages = useSessionStore((s) => s.messages);
  // Project audit-grade message rows onto user-visible turns. One bubble per
  // turn — see ./turns.ts for the grouping rules. Memoised on the messages
  // reference because the store updates the array on append, not in place.
  const chatTurns = useMemo(
    () => groupIntoTurns(messages),
    [messages],
  );
  // Last complete agent turn id — the inline-source summary attaches to this
  // turn's bubble. null when no complete agent turn exists yet (e.g. fresh
  // session, mid-flight first turn, or session-restore before any chat); the
  // standalone fallback widget further down handles those cases. Recomputed
  // on every chatTurns change since the value rolls forward across turns.
  const inlineSourceTargetTurnId = useMemo<string | null>(() => {
    for (let i = chatTurns.length - 1; i >= 0; i--) {
      const t = chatTurns[i];
      if (t.kind === "agent" && t.isComplete) return t.id;
    }
    return null;
  }, [chatTurns]);
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  const compositionState = useSessionStore((s) => s.compositionState);
  const compositionProposals = useSessionStore((s) => s.compositionProposals);
  const staleProposalIds = useSessionStore((s) => s.staleProposalIds);
  const proposalActionPendingIds = useSessionStore(
    (s) => s.proposalActionPendingIds,
  );
  const acceptProposal = useSessionStore((s) => s.acceptProposal);
  const rejectProposal = useSessionStore((s) => s.rejectProposal);
  const composerProgress = useSessionStore((s) => s.composerProgress);
  const clearError = useSessionStore((s) => s.clearError);
  const forkFromMessage = useSessionStore((s) => s.forkFromMessage);
  // Honest completion labels (elspeth-bf9c296ee5), derived from the run
  // gate's own signals rather than the generic terminal phase.
  const lastComposeChangedPipeline = useSessionStore(
    (s) => s.lastComposeChangedPipeline,
  );
  const freeformCompletionOutcome = useCompletionOutcome(
    activeSessionId,
    lastComposeChangedPipeline ?? true,
  );
  // Bootstrap-race gate shared with composer sends.
  const composeTimeoutReady = useSessionStore((s) => s.composeTimeoutReady);
  // Stuck state (backend up but reported no compose timeout): drives the
  // Explain button's disabled reason so it matches the main Send instead of
  // saying "Connecting…" forever.
  const composerTimeoutUnavailable = useSessionStore(
    (s) => s.composerTimeoutUnavailable,
  );
  // The same pending cards rendered by AcknowledgementStack feed the decision panel.
  const pendingAcknowledgementEvents = usePendingAcknowledgements(
    activeSessionId ?? "",
  );
  // A persisted handoff notice is actionable only while its review events
  // remain pending. Keep the notice's projection tied to loaded review state.
  const reviewEventsLoaded = useInterpretationEventsStore((state) =>
    activeSessionId !== null && state.pendingBySession[activeSessionId] !== undefined,
  );
  const pendingReviewCreatedAt = useMemo(
    () => reviewEventsLoaded
      ? pendingAcknowledgementEvents.map((event) => event.created_at)
      : undefined,
    [reviewEventsLoaded, pendingAcknowledgementEvents],
  );
  // Unsent drafts are keyed by session id (elspeth-ca38667856): ChatPanel
  // stays mounted across session switches, so a bare useState draft typed on
  // session A would still be sitting in session B's composer. Each session
  // owns a slot — switching away hides the draft, switching back restores it.
  // Clearing on switch instead would destroy typed content, which the
  // retention doctrine below forbids. The "" key carries the draft typed
  // while no session is active.
  const draftSessionKey = activeSessionId ?? "";

  const {
    sendMessage,
    retryMessage,
    cancelComposition,
    isComposing,
    error,
    errorDetails,
  } = composer;

  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  // The docked chrome's scroll container (.chat-panel-dock) — named so the
  // proposal-arrival reveal can scroll IT, and only it. Mutable (| null)
  // because attachDock assigns it from a callback ref.
  const dockRef = useRef<HTMLDivElement | null>(null);
  const [showScrollButton, setShowScrollButton] = useState(false);
  const [showBlobManager, setShowBlobManager] = useState(false);
  const [freeformDraftsBySession, setFreeformDraftsBySession] = useState<
    ReadonlyMap<string, string>
  >(new Map());
  const inputText = freeformDraftsBySession.get(draftSessionKey) ?? "";
  const setInputText = useCallback(
    (action: SetStateAction<string>) =>
      setFreeformDraftsBySession((drafts) =>
        withSessionDraftSlot(drafts, draftSessionKey, action),
      ),
    [draftSessionKey],
  );
  // Freeform upload ownership fence (elspeth-341a3e2fc4): a slow upload
  // started in session A may complete after the user switches to session B.
  // Without the fence ChatInput appends the LIVE (B) text plus the upload
  // sentence through its session-A-bound onChange closure — mixing B's
  // typed draft into A's slot. Accepting (true) keeps ChatInput's normal
  // live append (still on A); refusing (false) suppresses it, and the
  // slot-targeted append below carries the retention duty into the
  // originating session's draft instead — the same doctrine as the
  // failed-send restore (elspeth-49b467d91a).
  const handleFreeformBlobUploadCompleted = useCallback(
    (_requestId: string, sessionId: string, blob: BlobMetadata): boolean => {
      if (blob.session_id !== sessionId) {
        // Custody mismatch — never surface the blob anywhere.
        return false;
      }
      const liveSessionId = useSessionStore.getState().activeSessionId ?? "";
      if (liveSessionId === sessionId) {
        return true;
      }
      setFreeformDraftsBySession((drafts) =>
        withSessionDraftSlot(drafts, sessionId, (current) =>
          current +
          (current ? "\n" : "") +
          uploadedBlobPromptSentence(blob.filename),
        ),
      );
      return false;
    },
    [],
  );
  // A late failure belongs to the originating session too: suppress the
  // in-composer alert when another session is active (the blob manager
  // retains the failure detail for the owning session).
  const handleFreeformBlobUploadRejected = useCallback(
    (_requestId: string, sessionId: string): boolean =>
      (useSessionStore.getState().activeSessionId ?? "") === sessionId,
    [],
  );
  const activeComposerMessage = findActiveComposerMessage(messages);
  const proposalsByToolCallId = useMemo(
    () =>
      new Map(
        compositionProposals.map((proposal) => [
          proposal.tool_call_id,
          proposal,
        ]),
      ),
    [compositionProposals],
  );
  // Mid-flight tool activity for the ComposingIndicator's live tool log
  // (elspeth-3c2caf56a7). The atomic-reveal gate below hides the incomplete
  // tail agent turn's bubble, but its aggregatedToolCalls already carry the
  // per-call rows the inflight-messages poll delivers mid-turn — a read-only
  // projection; turn completeness semantics stay untouched. Empty once the
  // genuine reply lands or a new user row becomes the tail (fresh request).
  const liveToolCalls = useMemo(() => {
    const tail = chatTurns[chatTurns.length - 1];
    return isComposing && tail !== undefined && tail.kind === "agent" && !tail.isComplete
      ? tail.aggregatedToolCalls
      : [];
  }, [chatTurns, isComposing]);

  const scrollTranscriptToEnd = useCallback(() => {
    const container = scrollContainerRef.current;
    if (container === null) return;
    container.scrollTo({
      top: container.scrollHeight,
      behavior: preferredScrollBehavior(),
    });
  }, []);

  function scrollToBottom() {
    scrollTranscriptToEnd();
    setShowScrollButton(false);
  }

  // Track whether the user has scrolled up from the bottom
  function handleScroll() {
    const container = scrollContainerRef.current;
    if (!container) return;
    const threshold = 40; // pixels from bottom
    const atBottom =
      container.scrollHeight - container.scrollTop - container.clientHeight <
      threshold;
    setShowScrollButton(!atBottom);
  }

  // The terminal snapshot is deliberately retained after a turn settles so
  // this indicator bridges the gap until the reply renders (sessionStore:
  // "composerProgress as the live visible affordance until the final
  // assistant text lands"). That contract has a second half that was never
  // implemented — it must RETIRE once the text lands. Nothing else clears
  // composerProgress until the NEXT compose calls
  // startComposerProgressPolling, so a bare `|| isTerminal` kept the
  // indicator mounted indefinitely; docked as a sibling of the messages log
  // inside the composer's flex column, it held its full completed-turn
  // height in the input's space until the user sent again. A failed or
  // cancelled turn lands no complete agent reply, so its outcome still shows.
  const terminalComposerProgressRetired = useMemo(() => {
    const requestId = composerProgress?.request_id;
    if (requestId !== null && requestId !== undefined) {
      // Freeform progress.request_id is the persisted user-message id. Bind
      // retirement to that request rather than to whichever row happens to be
      // the absolute tail: validation can append a standalone system notice
      // after the reply, while an older assistant must not retire a newer
      // failed/cancelled request that produced no reply.
      const requestTurnIndex = chatTurns.findIndex(
        (turn) => turn.kind === "user" && turn.id === requestId,
      );
      if (requestTurnIndex !== -1) {
        for (let i = requestTurnIndex + 1; i < chatTurns.length; i++) {
          const turn = chatTurns[i];
          // A later user turn supersedes this snapshot. This can happen when a
          // new request fails before publishing progress and its final
          // one-shot load sees the previous request's terminal snapshot.
          if (turn.kind === "user") return true;
          if (turn.kind === "agent" && turn.isComplete) return true;
        }
        return false;
      }

      const tail = chatTurns[chatTurns.length - 1];
      return !(
        tail !== undefined &&
        tail.kind === "user" &&
        (tail.primaryMessage.local_status === "pending" ||
          tail.primaryMessage.local_status === "failed")
      );
    }
    // request_id is nullable for the idle snapshot and for failures before a
    // user row is persisted. Preserve the original tail-derived fallback for
    // those uncorrelatable states.
    const tail = chatTurns[chatTurns.length - 1];
    return tail !== undefined && tail.kind === "agent" && tail.isComplete;
  }, [chatTurns, composerProgress?.request_id]);
  const shouldShowComposerProgress =
    isComposing ||
    (isTerminalComposerPhase(composerProgress?.phase) &&
      !terminalComposerProgressRetired);

  // Auto-scroll to bottom when new messages arrive (unless user scrolled up).
  // Empty sessions render template cards above the sentinel; scrolling to the
  // bottom on first paint clips the top row of cards under the header.
  useEffect(() => {
    if (messages.length === 0 && !isComposing) return;
    if (!showScrollButton) {
      scrollTranscriptToEnd();
    }
  }, [messages, isComposing, showScrollButton, scrollTranscriptToEnd]);

  // Return focus to input when composing ends only if focus stayed in the
  // composer. Do not steal focus from proposal buttons, recovery actions, or
  // side-rail controls the user reached for while the request was running.
  useEffect(() => {
    const active = document.activeElement;
    const safeToRestore =
      active === null ||
      active === document.body ||
      active === inputRef.current;
    if (!isComposing && safeToRestore) {
      inputRef.current?.focus();
    }
  }, [isComposing]);

  // Reset scroll state when switching sessions
  useEffect(() => {
    setShowScrollButton(false);
  }, [activeSessionId]);













  // ── Inline-source projection (Phase 5a Task 3) ─────────────────────────────
  //
  // When active source blob refs resolve to assistant-created blobs with
  // chat-message provenance, project each blob's metadata and
  // a bounded content preview into the inlineSourceStore. The summary is rendered
  // inside the agent bubble (MessageBubble's "Sources created" disclosure
  // group) — the store is the projection layer for downstream consumers
  // (Task 4 disambiguation widget, Task 7 audit-readiness row, and the
  // bubble's sources-created group).
  //
  // The effect is cancelled via a `cancelled` flag set in the cleanup so that
  // a session-switch or composition-replace mid-fetch does not race the
  // older response into the newer summary slot.
  //
  // Do not use creation_modality alone as the predicate: ordinary uploaded
  // files and assistant-created inline blobs may both be `verbatim`. The
  // backend distinguishes chat-created inline blobs by `created_by=assistant`
  // plus a non-null `created_from_message_id`; browser uploads and pipeline
  // outputs do not carry that pair. Checking metadata before preview fetch
  // keeps large uploaded sources out of the chat-created-source projection.
  const blobRefsKey = JSON.stringify(readBlobRefs(compositionState));
  const blobRefs = useMemo<string[]>(() => JSON.parse(blobRefsKey), [blobRefsKey]);
  const [sourceProjectionFailure, setSourceProjectionFailure] = useState<{
    sessionId: string; blobRefsKey: string;
  } | null>(null);
  const [sourceProjectionAttempt, setSourceProjectionAttempt] = useState(0);
  const setInlineSourceSummary = useInlineSourceStore((s) => s.setSummary);
  const retainInlineSourceSummaries = useInlineSourceStore((s) => s.retainSummaries);
  const storedInlineSources = useInlineSourceStore((s) => s.summariesBySession);
  const inlineSourceSummaries = activeSessionId === null ? [] :
    blobRefs.flatMap((blobId) =>
      (storedInlineSources[activeSessionId] ?? []).filter((summary) => summary.blobId === blobId),
    );


  // ── Interpretation review resolve-success confirmation (Phase 5b.18b.8) ──
  //
  // The interpretation-review widget (InterpretationReviewInlineMessage)
  // unmounts on successful resolve because the parent re-renders with the
  // event removed from `pendingBySession`. The widget's onResolved callback
  // is therefore the only signal we can use to push a confirmation line
  // back into the chat after dismissal — by the time the next render runs,
  // `event.user_term` is no longer reachable.
  //
  // Spec lines 768-774: "Got it — using your interpretation of *<user_term>*."
  // We render this as an assistant-styled chat bubble inside the
  // message-stream region so the user sees a natural continuation of the
  // conversation. The confirmations are LOCAL UI state — NOT pushed to
  // sessionStore.messages and NOT written to the audit trail (the audit
  // trail's interpretation_event row is the canonical record; this is the
  // human-readable echo, an explicit UI nudge per spec line 772).
  //
  // Each confirmation carries a local id so React reconciliation keeps
  // confirmation bubbles stable when new events resolve while older
  // confirmations are still visible. Confirmations persist for the
  // lifetime of the ChatPanel mount; switching sessions or reloading clears
  // them, which matches the "ephemeral UI nudge" intent.
  // Confirmations are DERIVED from the resolved interpretation events, not
  // accumulated in component state (elspeth-51ed4fd8d5). They used to be a
  // local useState list appended on each resolve and rendered after the whole
  // turn stream, which produced two defects at once:
  //
  //   Position — append order IS the position, so a confirmation was
  //   permanently last. Resolve a card, send another message, and the "Got
  //   it" sat below that message reading as a reply to it; three resolutions
  //   piled up as a block at the tail no matter which turns raised them.
  //
  //   Persistence — the list was seeded [] and reset on session switch, never
  //   hydrated, so a reload erased every confirmation. In an audit-first
  //   product the transcript then shows the assumptions being surfaced and
  //   never shows the operator approving them.
  //
  // interpretationEventsStore.resolvedBySession fixes both: refreshAll
  // populates it from the wire on session load and resolveEvent appends to it
  // live, so one source serves both paths, and every row carries the
  // tool_call_id that anchors it to the turn that raised the term.
  const resolvedInterpretations = useInterpretationEventsStore((state) =>
    activeSessionId === null
      ? NO_RESOLVED_INTERPRETATIONS
      : (state.resolvedBySession[activeSessionId] ??
        NO_RESOLVED_INTERPRETATIONS),
  );

  // Only operator approvals may render a "Got it" — classified by `choice`
  // via the store's exported selector, NOT by user_term presence: the
  // surface-specific auto_interpreted_opt_out shape is CHECK-required to
  // carry a user_term and tool_call_id, so field presence marks a declined
  // review as readily as an approval (elspeth-3a8a843c47).
  const approvedInterpretations = useMemo(
    () => selectApprovedInterpretations(resolvedInterpretations),
    [resolvedInterpretations],
  );

  // toolCallId -> the approvals resolved against that call, in resolution
  // order. Each entry keeps the event id and affected node
  // (elspeth-52be5924d7): the same user_term can be approved twice under one
  // turn (per-node staging dedup + fixed pipeline_decision constants), and
  // the backend's pending-uniqueness index puts those approvals on distinct
  // tool_call_ids — so the term alone can neither key the bubbles nor tell
  // them apart. The event id is the render key; the node is the visible
  // differentiator.
  //
  // Approved rows always carry a user_term (the backend CHECK on
  // user_approved rows guarantees it); the null/empty guard is wire-type
  // narrowing, not classification. Rows with a term but no tool_call_id
  // cannot be anchored and fall through to `unanchoredConfirmations` below
  // rather than being dropped — an approval the operator gave is not
  // something to discard because we cannot place it.
  const confirmationsByToolCallId = useMemo(() => {
    const byToolCall = new Map<
      string,
      {
        id: string;
        userTerm: string;
        kind: InterpretationEvent["kind"];
        affectedNodeId: string | null;
        resolvedAt: string | null;
      }[]
    >();
    for (const event of approvedInterpretations) {
      const userTerm = event.user_term;
      if (userTerm === null || userTerm === "") continue;
      const toolCallId = event.tool_call_id;
      if (toolCallId === null) continue;
      const approval = {
        id: event.id,
        userTerm,
        kind: event.kind,
        affectedNodeId: event.affected_node_id,
        resolvedAt: event.resolved_at,
      };
      const approvals = byToolCall.get(toolCallId);
      if (approvals === undefined) byToolCall.set(toolCallId, [approval]);
      else approvals.push(approval);
    }
    return byToolCall;
  }, [approvedInterpretations]);

  // The turns that actually reach the DOM. Hoisted out of the JSX because the
  // confirmation anchoring has to know which tool calls are on screen: the
  // atomic-reveal gate hides a mid-flight tail turn, and a confirmation whose
  // anchor is hidden must fall to the tail rather than silently vanish.
  const renderedTurns = useMemo(
    () =>
      chatTurns.filter(
        (turn: ChatTurn, index: number) =>
          turn.isComplete || !isComposing || index < chatTurns.length - 1,
      ),
    [chatTurns, isComposing],
  );

  const tailConfirmations = useMemo(() => {
    const onScreen = new Set<string>();
    for (const turn of renderedTurns) {
      for (const call of turn.aggregatedToolCalls) onScreen.add(call.id);
    }
    const tail: {
      id: string;
      userTerm: string;
      kind: InterpretationEvent["kind"];
      affectedNodeId: string | null;
      resolvedAt: string | null;
    }[] = [];
    for (const event of approvedInterpretations) {
      const userTerm = event.user_term;
      if (userTerm === null || userTerm === "") continue;
      const toolCallId = event.tool_call_id;
      if (toolCallId !== null && onScreen.has(toolCallId)) continue;
      tail.push({
        id: event.id,
        userTerm,
        kind: event.kind,
        affectedNodeId: event.affected_node_id,
        resolvedAt: event.resolved_at,
      });
    }
    return tail;
  }, [approvedInterpretations, renderedTurns]);

  const markFallbackDismissed = useInlineSourceStore((s) => s.markDismissed);
  // Subscribe through the dismissedAt Map so the predicate re-evaluates
  // when a dismissal lands. Calling `isDismissed(sessionId)` inside the
  // selector body itself would not trigger a re-render on store change
  // (the selector returns a primitive boolean derived from the Map but
  // doesn't subscribe to the Map's identity change unless we read the
  // Map directly).
  const fallbackDismissedAt = useInlineSourceStore((s) => s.dismissedAt);

  useEffect(() => {
    if (activeSessionId === null) return;
    retainInlineSourceSummaries(activeSessionId, blobRefs);
    setSourceProjectionFailure(null);
    let cancelled = false;
    const sessionId = activeSessionId;
    void Promise.all(blobRefs.map(async (targetBlobId) => {
      try {
        const meta = await getBlobMetadata(sessionId, targetBlobId);
        if (cancelled || !isInlineSourceBlob(meta)) return;
        const text = await previewBlobContent(sessionId, targetBlobId);
        if (cancelled) return;
        const summary = await projectInlineSourceSummary({
          metadata: meta,
          contentText: text,
          toProvenance: toInlineSourceProvenance,
        });
        if (!cancelled) setInlineSourceSummary(sessionId, summary);
      } catch (err) {
        if (!cancelled) {
          console.error("[inline-source] projection failed:", err);
          setSourceProjectionFailure({ sessionId, blobRefsKey });
        }
      }
    }));
    return () => { cancelled = true; };
  }, [
    activeSessionId, blobRefs, blobRefsKey, sourceProjectionAttempt,
    setInlineSourceSummary, retainInlineSourceSummaries,
  ]);

  const actionableBannerProposalIds = useMemo(
    () => actionableProposals(compositionProposals).map((p) => p.id),
    [compositionProposals],
  );
  const seenActionableBannerIdsRef = useRef<ReadonlySet<string>>(new Set());
  const revealActionableProposals = useCallback(
    (revealAlreadySeen: boolean) => {
      const dock = dockRef.current;
      // A loading surface may not yet have attached its decision dock.
      if (dock === null) return;
      const seen = seenActionableBannerIdsRef.current;
      seenActionableBannerIdsRef.current = new Set(actionableBannerProposalIds);
      const shouldReveal = revealAlreadySeen
        ? actionableBannerProposalIds.length > 0
        : actionableBannerProposalIds.some((id) => !seen.has(id));
      if (!shouldReveal) return;
      const banner = dock.querySelector<HTMLElement>(".decision-panel-item--pending_proposal");
      if (banner === null) return;
      const bannerTop =
        banner.getBoundingClientRect().top -
        dock.getBoundingClientRect().top +
        dock.scrollTop;
      dock.scrollTo({ top: bannerTop, behavior: preferredScrollBehavior() });
    },
    [actionableBannerProposalIds],
  );
  useEffect(() => {
    revealActionableProposals(false);
  }, [revealActionableProposals]);
  const revealActionableProposalsRef = useRef(revealActionableProposals);
  revealActionableProposalsRef.current = revealActionableProposals;
  const attachDock = useCallback((node: HTMLDivElement | null) => {
    dockRef.current = node;
    if (node === null) return;
    revealActionableProposalsRef.current(true);
  }, []);

  // ── Inline-source fallback predicate (Phase 5a Task 5) ───────────────────
  //
  // The fallback prompt fires when ALL of:
  //   1. User has sent ≥1 user message in the session.
  //   2. No source is bound on the composition state.
  //   3. The most recent user message that survives `looksLikeData` is
  //      the actual candidate (we walk the last few user messages so a
  //      transient question turn doesn't suppress the affordance when a
  //      prior URL is still the unresolved input).
  //   4. The composer is not currently responding to that user message.
  //      The fallback is a post-turn safety net, not a mid-compose
  //      competing offer.
  //   5. No source-related tool call is in flight on the latest
  //      assistant message (set_pipeline, set_source_from_blob,
  //      set_source). If one is in flight the LLM is mid-response and
  //      we must not race the affordance against the proposal pipeline.
  //   6. The fallback has not been dismissed for this session (F-20).
  //
  // The candidate is the most recent looksLikeData-positive user message
  // text, walking backwards through the LAST 3 user messages (a 3-turn
  // window — wider than 1 lets a "what does it cost?" follow-up question
  // not suppress the affordance for a still-unresolved URL above it; the
  // spec mentions N=2 turns, we use 3 for the same reason). Older
  // unresolved candidates fade out naturally as the chat scrolls.
  const fallbackCandidate = useMemo(() => {
    const userMessages = messages.filter((m) => m.role === "user");
    if (userMessages.length === 0) return null;
    const recent = userMessages.slice(-3).reverse();
    for (const m of recent) {
      if (looksLikeData(m.content)) return m.content;
    }
    return null;
  }, [messages]);

  // Inflight source-tool-call check — gate on the LATEST assistant
  // message's tool_calls. The ToolCall wire shape carries the function
  // name at `tc.function.name` (LiteLLM convention; see types/index.ts).
  //
  // CLOSED LIST — the three source-mutating tool names. Adding a fourth
  // source-mutating tool to the composer means widening this set; the
  // CLOSED-LIST framing prevents quiet drift.
  const inflightSourceToolNames: ReadonlySet<string> = useMemo(
    () => new Set(["set_pipeline", "set_source_from_blob", "set_source"]),
    [],
  );
  const hasInflightSourceCall = useMemo(() => {
    const lastAssistant = [...messages]
      .reverse()
      .find((m) => m.role === "assistant");
    if (!lastAssistant) return false;
    const calls = lastAssistant.tool_calls ?? [];
    return calls.some((tc) => inflightSourceToolNames.has(tc.function.name));
  }, [messages, inflightSourceToolNames]);

  // Source-bound predicate. The shape mirrors the spec: either no
  // composition state OR no named source OR every source plugin slot is the
  // empty string (the composer's pre-source-bound representation).
  const compositionHasSource =
    compositionState !== null &&
    Object.values(compositionState.sources).some((source) => source.plugin !== "");

  // F-20 session-scoped dismissal. The store action `markDismissed`
  // populates `dismissedAt[sessionId]`; we read via the Map identity
  // we subscribed to above so the predicate re-evaluates on flip.
  const sessionDismissed =
    activeSessionId !== null && fallbackDismissedAt.has(activeSessionId);


  const shouldRenderFallback =
    fallbackCandidate !== null &&
    !isComposing &&
    !hasInflightSourceCall &&
    !compositionHasSource &&
    !sessionDismissed;

  // ── Decision panel (elspeth-cb0d4b8dba) ──────────────────────────────────
  // The one "Awaiting your decision" surface above the input. Every input is
  // an existing store fact: the durable readiness gate the server re-emits on
  // each validate, the composition's validator suggestions, the pending
  // review cards, and the proposals the banner already showed here. The
  // projection is pure (decisionPanelRows.ts); the panel is a dumb render;
  // suggestion/fallback handlers send provider chat; proposal and
  // interpretation decisions use their existing approval APIs.
  const validationResult = useExecutionStore((s) => s.validationResult);
  const pendingApproval = useExecutionStore((s) => s.pendingApproval);
  const decisionRows = useMemo(
    () =>
      projectDecisionRows({
        validationResult,
        compositionState,
        pendingInterpretations: pendingAcknowledgementEvents,
        proposals: compositionProposals,
        inlineSourceCandidate: shouldRenderFallback ? fallbackCandidate : null,
      }),
    [
      validationResult,
      compositionState,
      pendingAcknowledgementEvents,
      compositionProposals,
      shouldRenderFallback,
      fallbackCandidate,
    ],
  );
  const decisionPhraseFor = useMemo(
    () => makePhraseFor(compositionState),
    [compositionState],
  );
  const decisionStepLabelFor = useCallback(
    (componentId: string): string | null =>
      stepLabelForNodeId(compositionState, componentId),
    [compositionState],
  );
  // Same gate as the side rail's SuggestionList: a send started before the
  // backend compose wall clock lands at boot could be aborted before the
  // backend's 422 (bootstrap race), so Apply stays closed until
  // composeTimeoutReady, and reads as connecting (or the stuck unavailable
  // state) rather than as a dead click.
  const decisionApplyDisabled = isComposing || !composeTimeoutReady;
  const decisionApplyDisabledReason = composerTimeoutUnavailable
    ? COMPOSE_UNAVAILABLE_MESSAGE
    : COMPOSE_CONNECTING_MESSAGE;
  const handleApplySuggestion = useCallback(
    (suggestion: ValidationEntryDTO) => {
      void sendMessage(applySuggestionPrompt(suggestion));
    },
    [sendMessage],
  );
  const handleRepairGraph = useCallback(() => {
    if (decisionApplyDisabled || validationResult === null) return;
    void sendMessage(repairGraphPrompt(validationResult.errors));
  }, [decisionApplyDisabled, sendMessage, validationResult]);
  const handleAskAboutBlocker = useCallback(
    (detail: string, componentId: string | null, note: string | null) => {
      const stepPhrase = componentId === null ? null : decisionPhraseFor(componentId);
      setInputText(askAboutBlockerDraft(detail, stepPhrase, note));
      queueMicrotask(() => inputRef.current?.focus());
    },
    [decisionPhraseFor, setInputText],
  );
  const decisionAskDisabledReason =
    inputText.trim() === "" ? null : "Send or clear your draft before asking about a blocker.";
  const handleOpenChecks = useCallback(() => {
    dispatchArtifactViewIntent({
      tab: "checks",
      focusMode: false,
      sessionId: activeSessionId,
    });
  }, [activeSessionId]);
  // F-3 — no API jargon in the user-visible chat message. The dispatched
  // chat turn reads as natural language; the composer prompt (Task 8)
  // teaches the LLM to recognise this framing and call set_pipeline
  // with an inline_blob source. The fallback path goes through the
  // SAME tool-use loop as the LLM-initiated path, which is what
  // preserves audit-trail equivalence.
  const handleFallbackAccept = useCallback(
    (text: string) => {
      sendMessage(`Use this as my source data:\n\n${text}`);
    },
    [sendMessage],
  );

  const handleFallbackDismiss = useCallback(() => {
    if (activeSessionId !== null) {
      markFallbackDismissed(activeSessionId);
    }
  }, [activeSessionId, markFallbackDismissed]);

  /**
   * "Edit the list" handler (Phase 5a Task 3 v1).
   *
   * v1 emits a conversational instruction to the composer LLM, pre-loaded
   * with the current preview so the user can amend in the chat textarea.
   * This is the minimal viable path: the composer's `set_pipeline` tool
   * accepts an `inline_blob.content` re-write, so an LLM-mediated edit goes
   * through the existing proposal-approval pipeline (same audit-event
   * lineage as the original creation).
   *
   * A direct in-browser textarea modal is the natural Task 6+ follow-up —
   * but it requires a `setPipeline` action surface in `sessionStore`, which
   * does not exist today (all pipeline mutations flow through the LLM
   * tool-use loop). Shipping the textarea modal here would mean adding that
   * surface, which is out of scope for Task 3. The chat-mediated handler
   * lets Task 6's integration test exercise the click path; the modal
   * upgrade is tracked separately.
   */
  const handleEditInlineSource = useCallback(
    (summary: InlineSourceSummary) => {
      const sourceNames = compositionState === null ? [] : sortedSourceEntries(compositionState)
        .filter(([, source]) => readSourceBlobRef(source) === summary.blobId)
        .map(([name]) => name);
      if (sourceNames.length === 0) return;
      const prompt =
        `I'd like to edit the inline source "${summary.filename}". ` +
        `Pipeline source names: ${sourceNames.join(", ")}. Blob ID: ${summary.blobId}. ` +
        `Current contents:\n\n${summary.contentPreview}\n\n` +
        `Please update it per the changes I describe in my next message.`;
      sendMessage(prompt);
    },
    [sendMessage, compositionState],
  );




  const handleSend = useCallback(
    (content: string) => {
      sendMessage(content);
      // Explicit send means user has returned to live conversation —
      // force-scroll to bottom and resume auto-scroll.
      setShowScrollButton(false);
      scrollTranscriptToEnd();
    },
    [sendMessage, scrollTranscriptToEnd],
  );

  const handleFork = useCallback(
    (messageId: string, newContent: string) => {
      forkFromMessage(messageId, newContent);
    },
    [forkFromMessage],
  );

  const handleUseAsInput = useCallback(
    (blob: BlobMetadata) => {
      // Insert a helper message referencing the blob by filename.
      // The assistant/composer will use blob tools to wire it as source.
      const prompt = `Please use the file "${blob.filename}" as the pipeline input.`;
      sendMessage(prompt);
      setShowBlobManager(false);
    },
    [sendMessage],
  );

  // No active session: show prompt to select or create one
  if (!activeSessionId) {
    return (
      <div
        id="chat-main"
        className="chat-panel chat-panel--empty"
        role="region"
        aria-label="Chat panel"
      >
        Use the session switcher to select a session or create a new one.
      </div>
    );
  }

  // One persistent announcer owns every pending decision across modes.
  const decisionLiveRegion = (
    <DecisionPanelLiveRegion
      count={decisionRows.count}
      decisionIds={decisionRows.rows.map((row) => row.id)}
    />
  );
  // A pending approval withholds execution, so it is a blocking state and belongs
  // at the top level beside the decision panel — never behind the Checks sub-tab
  // (operator placement ruling). It is rendered separately rather than as a
  // DecisionPanel row because that panel returns null with nothing to decide,
  // which is exactly when a clean composition is ready to be sent for approval.
  // The row self-hides unless workflow governance is on.
  const approvalReadiness =
    activeSessionId !== null && compositionState !== null ? (
      <ApprovalReadinessRow
        sessionId={activeSessionId}
        stateId={compositionState.id}
        pendingApproval={pendingApproval}
      />
    ) : null;
  const decisionPanel = (
    <>
      <DecisionPanel
        rows={decisionRows.rows}
        blockedVerbs={decisionRows.blockedVerbs}
        count={decisionRows.count}
        proposals={compositionProposals}
        staleProposalIds={staleProposalIds}
        proposalActionPendingIds={proposalActionPendingIds}
        isComposing={isComposing}
        applyDisabled={decisionApplyDisabled}
        applyDisabledReason={decisionApplyDisabledReason}
        phraseFor={decisionPhraseFor}
        stepLabelFor={decisionStepLabelFor}
        onApplySuggestion={handleApplySuggestion}
        onAskAboutBlocker={handleAskAboutBlocker}
        onRepairGraph={handleRepairGraph}
        askDisabledReason={decisionAskDisabledReason}
        onOpenChecks={handleOpenChecks}
        onAcceptProposal={acceptProposal}
        onRejectProposal={rejectProposal}
        onEmptyFocus={() => inputRef.current?.focus()}
        interpretationContent={
          <AcknowledgementStack
            sessionId={activeSessionId}
            onFocusFallback={() => inputRef.current?.focus()}
            onResolved={createInterpretationResolutionHandler(activeSessionId)}
          />
        }
        renderSourceFallback={(candidateText) => (
          <InlineSourceFallbackPrompt
            shouldRender
            candidateText={candidateText}
            onAccept={handleFallbackAccept}
            onDismiss={handleFallbackDismiss}
          />
        )}
      />
    </>
  );








  return (
    // role="region" so the aria-label is exposed as a named landmark —
    // aria-label on a role-less div is ignored by AT (WCAG 1.3.1,
    // elspeth-37293a3b7c). Applies to every id="chat-main" branch above too.
    <div
      id="chat-main"
      className="chat-panel"
      role="region"
      aria-label="Chat panel"
      // data-composing surfaces the "agent is thinking" state to CSS so the
      // textarea and send-button cursors flip to `progress` while the compose
      // request is in-flight. The ComposingIndicator block below is the
      // primary affordance; the cursor change reinforces "system is busy"
      // for users whose pointer is hovering the input area. See
      // components/chat/chat.css [data-composing="true"] rules.
      data-composing={isComposing ? "true" : undefined}
    >
      {decisionLiveRegion}
      {/* Persistent composer authority. Mode preferences live in Preferences. */}
      <div className="chat-panel-header">
        {/* Layout lives in chat.css, NOT in a style prop (elspeth-0b70269ccc).
            As an inline style this row was `inline-flex` with no wrap and no
            min-width, which no stylesheet rule and therefore no breakpoint
            could override — the row stayed one line at every width and the
            ModeSwitchButton clipped to "Sw" at 390px. */}
        <div className="chat-panel-header-actions">
          {/* Persistent composer authority (elspeth-f5e6723133): whether
              this session auto-applies mutations or gates them behind
              proposals — named in the chrome. */}
          <AuthorityChip />
        </div>
      </div>

      {/* Error banner. Renders the primary error message plus, when
          present, a bulleted list of structured `errorDetails` (currently
          populated from `validation_errors` on a proposal-accept failure).
          Without the bullets the toast collapses Pydantic's flattened
          error string into one unreadable line. */}
      {error && (
        <div role="alert" className="chat-panel-error">
          <div className="chat-panel-error-body">
            <p className="chat-panel-error-message">{error}</p>
            {errorDetails && errorDetails.length > 0 && (
              <ul className="chat-panel-error-details">
                {errorDetails.map((detail, idx) => (
                  <li key={idx}>{detail}</li>
                ))}
              </ul>
            )}
          </div>
          <Button
            variant="bare"
            onClick={clearError}
            className="chat-panel-error-dismiss"
            aria-label="Dismiss error"
          >
            {"\u00D7"}
          </Button>
        </div>
      )}

      {/* Messages region (elspeth-4ad68a3769): the positioning containing
          block for the jump-to-latest pill. The pill lives INSIDE this
          region (so its bottom edge tracks the messages area, clearing the
          indicator/banners/ChatInput at every pane width) but OUTSIDE the
          scrolling element (so it floats instead of scrolling away). */}
      <div className="chat-panel-messages-region">
        {/* Message list.
            tabIndex=0 (elspeth-5e43a0c8b2, WCAG 2.1.1): the scroll container
            must be keyboard-focusable so keyboard-only users can arrow-scroll
            a long conversation instead of tabbing through every interactive
            child. The focus ring is the app's :focus-visible idiom, drawn
            inset in chat.css because .chat-panel clips overflow. role="log"
            aria-live semantics are unchanged. */}
        <div
          ref={scrollContainerRef}
          onScroll={handleScroll}
          className="chat-panel-messages"
          role="log"
          aria-label="Conversation"
          aria-live="polite"
          aria-relevant="additions"
          tabIndex={0}
        >
          {messages.length === 0 ? (
            <FreeformIntroduction />
          ) : (
            // Render one bubble per *turn*, not one per audit row. The compose
            // loop persists every LLM round-trip as its own assistant row
            // (Tier-1 audit doctrine); grouping projects the audit stream onto
            // user-visible turns so a single user prompt becomes one user
            // bubble + one agent bubble that aggregates every tool call and the
            // final answer. See ./turns.ts.
            //
            // Atomic-reveal gate: agent turns that are mid-flight (only
            // tool-call rows landed, no LLM text reply yet) are hidden from the
            // timeline. The ComposingIndicator (rendered further down while
            // `isComposing` is true) is the visible affordance for "the agent
            // is thinking" — leaking a half-assembled bubble on top of it
            // creates a confusing race between tool calls and the eventual
            // answer. User and system turns are always complete, so the gate
            // is a no-op for them. See turns.ts → ChatTurn.isComplete.
            //
            // `|| !isComposing` is the terminal escape (elspeth-e074575b6e).
            // A turn is only "mid-flight" while the composer is still running;
            // once isComposing goes false nothing more is coming, so an
            // incomplete turn is one that ENDED without a reply (convergence /
            // timeout — the backend persists partial state and the tool audit,
            // then raises 422, and never writes a reply row). A later turn also
            // proves an incomplete agent turn is historical, even while a new
            // composition is running. Those historical turns must still render
            // so their tool calls stay visible; only the current tail turn stays
            // behind the atomic-reveal gate.
            renderedTurns.map((turn: ChatTurn) => {
                const repr = turnRepresentativeMessage(turn);
                // Attach the inline-source summary to the most recent complete
                // agent turn. The summaries follow named-source order,
                // independent of the order their metadata requests finish.
                // When no agent turn is present (e.g.
                // session-restore loaded a composition before any chat), the
                // summary falls through to the standalone widget rendered
                // below the message stream.
                const sourcesForThisTurn =
                  inlineSourceSummaries.length > 0 && turn.id === inlineSourceTargetTurnId
                    ? inlineSourceSummaries
                    : undefined;
                // Interpretation confirmations raised BY this turn, emitted
                // straight after its bubble (elspeth-51ed4fd8d5). The anchor
                // is the tool call that surfaced the term, so the echo stays
                // beside the exchange it belongs to however long the operator
                // takes to resolve the card, and however many turns land in
                // between.
                const confirmedApprovals = turn.aggregatedToolCalls.flatMap(
                  (call) => confirmationsByToolCallId.get(call.id) ?? [],
                );
                return (
                  <Fragment key={turn.id}>
                    <MessageBubble
                      message={repr}
                      isComposing={isComposing}
                      onRetry={turn.kind === "user" ? retryMessage : undefined}
                      onFork={allowFork && turn.kind === "user" ? handleFork : undefined}
                      proposalsByToolCallId={proposalsByToolCallId}
                      compositionState={compositionState}
                      staleProposalIds={staleProposalIds}
                      sourcesCreated={sourcesForThisTurn}
                      onEditInlineSource={handleEditInlineSource}
                      pendingReviewCreatedAt={pendingReviewCreatedAt}
                    />
                    {/* The graph's Approvals table owns resolved history once
                        a composition exists; keep the chat echo only while no
                        graph table can render it. */}
                    {compositionState === null && confirmedApprovals.map((conf) => (
                      <InterpretationConfirmation
                        key={conf.id}
                        userTerm={conf.userTerm}
                        kind={conf.kind}
                        affectedNodeId={conf.affectedNodeId}
                        resolvedAt={conf.resolvedAt}
                      />
                    ))}
                  </Fragment>
                );
              })
          )}
          {/* Standalone fallback for the inline-source summary: only painted
              when no complete agent turn exists to absorb it into the bubble
              (e.g. session-restore where a composition was loaded before any
              chat turn happened). The hybrid keeps the operator's stated UX —
              sources-created appears inside the bubble like tool calls do —
              while not silently dropping the summary in pre-chat states. */}
          {inlineSourceTargetTurnId === null && inlineSourceSummaries.map((summary) => (
            <InlineSourceCreatedTurn
              key={summary.blobId}
              summary={summary}
              onEdit={handleEditInlineSource}
            />
          ))}
          {sourceProjectionFailure?.sessionId === activeSessionId &&
            sourceProjectionFailure?.blobRefsKey === blobRefsKey && (
            <div role="alert">
              <p>Could not load some source details.</p>
              <Button onClick={() => setSourceProjectionAttempt((attempt) => attempt + 1)}>
                Retry source details
              </Button>
            </div>
          )}
          {/*
            Unanchorable resolve confirmations (Phase 5b.18b.8).

            The anchored ones render beside the turn that raised them, up in
            the turn map. These are the residue: rows carrying no tool_call_id,
            or whose call is not on screen. When no composition is available,
            rendering them here keeps the approval visible despite the
            missing graph table and turn anchor.

            role="status" inside the role="log" region so an arriving bubble is
            announced (aria-live="polite" on the parent).

            The residue renders inside a LABELED SECTION, not as bare rows
            (elspeth-3574f87208, operator ruling 2026-09-01): three structural
            classes can never anchor — (A) backend-auto-surfaced events whose
            sentinel tool_call_id matches no provider call by construction,
            (B) revert/import/seed events bound to states no chat row
            references. Unsectioned, those rows read as assistant replies to the
            newest message and grow without bound (one per resolution,
            rehydrated on every load). The header carries the count — the
            per-row node clause stays (elspeth-52be5924d7), so no collapse.
          */}
          {compositionState === null && tailConfirmations.length > 0 && (
            <section
              className="interpretation-approvals-section"
              data-testid="interpretation-approvals-section"
              aria-label={`Interpretation approvals (${tailConfirmations.length})`}
            >
              <h3 className="interpretation-approvals-heading">
                Interpretation approvals ({tailConfirmations.length})
              </h3>
              {tailConfirmations.map((conf) => (
                <InterpretationConfirmation
                  key={conf.id}
                  userTerm={conf.userTerm}
                  kind={conf.kind}
                  affectedNodeId={conf.affectedNodeId}
                  resolvedAt={conf.resolvedAt}
                />
              ))}
            </section>
          )}
        </div>

        {/* Scroll-to-bottom button — sibling of the scrolling element, so it
            floats over the messages instead of scrolling away with them. */}
        {showScrollButton && (
          <Button
            onClick={scrollToBottom}
            aria-label="Scroll to bottom"
            className="scroll-to-bottom-btn"
          >
            {"\u2193"} Jump to latest
          </Button>
        )}
      </div>

      {/* Docked chrome: everything that sits between the transcript and the
          composer. It is ONE box (elspeth-ecf973fb9f) because the panel's
          vertical budget has to be settled between two claimants, not four:
          the transcript region above yields to zero (flex:1 with a 0 basis
          contributes nothing to shrinking), so before this wrapper existed a
          tall dock had nowhere to take space FROM and pushed .chat-input past
          the panel's own bottom edge — measured 130px below it, clipped away
          by the panel's clip, with the whole transcript gone as well. The
          composer is the panel's primary control and is never the thing that
          yields; the dock is. See .chat-panel-dock in chat.css.

          Grouping is layout-only: each child keeps the DOM position, order and
          live-region semantics it had as a direct panel child. */}
      {/* tabIndex=0 (WCAG 2.1.1): the dock is a scroll container whose hidden
          content need not contain anything focusable — measured 67px below
          the last focusable control, reachable by wheel or drag alone. Same
          ruling and same idiom as the transcript scroller above. The cost is
          one extra tab stop on the transcript-to-composer path; it is the
          smaller loss. Focus ring in chat.css (.chat-panel-dock:focus-visible). */}
      <div className="chat-panel-dock" tabIndex={0} ref={attachDock}>
        {/* Composing indicator — deliberately a SIBLING of the role="log"
            messages container, not a child (elspeth-76a0cc485e, WCAG 4.1.3):
            its role="status" is itself a polite live region, and nesting it
            inside the aria-live log risks double announcements on AT that
            honours both regions. Docked here it also stays visible while the
            user scrolls back through history mid-compose. */}
        {shouldShowComposerProgress && (
          <ComposingIndicator
            latestRequest={activeComposerMessage?.content ?? null}
            compositionState={compositionState}
            composerProgress={composerProgress}
            completionOutcome={freeformCompletionOutcome}
            liveToolCalls={liveToolCalls}
          />
        )}

        {/* Blob manager drawer */}
        {showBlobManager && <BlobManager onUseAsInput={handleUseAsInput} />}

        {decisionPanel}
          {approvalReadiness}
      </div>

      {/* Input */}
      <ChatInput
        onSend={handleSend}
        disabled={isComposing}
        onCancel={isComposing ? cancelComposition : undefined}
        inputRef={inputRef}
        onToggleBlobManager={() => setShowBlobManager((v) => !v)}
        showBlobManager={showBlobManager}
        onOpenSecrets={onOpenSecrets}
        value={inputText}
        onChange={setInputText}
        onBlobUploadCompleted={handleFreeformBlobUploadCompleted}
        onBlobUploadRejected={handleFreeformBlobUploadRejected}
      />
    </div>
  );
}

// Apply a setState-style action to one session's draft slot. Preserves map
// identity when the value is unchanged (no spurious re-render) and drops
// empty slots so the map only holds sessions with a live unsent draft.
function withSessionDraftSlot(
  drafts: ReadonlyMap<string, string>,
  sessionKey: string,
  action: SetStateAction<string>,
): ReadonlyMap<string, string> {
  const current = drafts.get(sessionKey) ?? "";
  const value = typeof action === "function" ? action(current) : action;
  if (value === current) return drafts;
  const next = new Map(drafts);
  if (value === "") {
    next.delete(sessionKey);
  } else {
    next.set(sessionKey, value);
  }
  return next;
}








function findActiveComposerMessage(messages: ChatMessage[]): ChatMessage | null {
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === "user" && message.local_status === "pending") {
      return message;
    }
  }
  for (let index = messages.length - 1; index >= 0; index -= 1) {
    const message = messages[index];
    if (message.role === "user") {
      return message;
    }
  }
  return null;
}
