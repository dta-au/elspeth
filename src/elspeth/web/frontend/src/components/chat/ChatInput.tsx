// src/components/chat/ChatInput.tsx
import {
  useId,
  useState,
  useCallback,
  useEffect,
  useRef,
  type KeyboardEvent,
  type ChangeEvent,
} from "react";
import { Button, Icon, Input } from "@/components/ui";
import { useSessionStore } from "@/stores/sessionStore";
import { useBlobStore } from "@/stores/blobStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { PREFILL_CHAT_INPUT_EVENT } from "@/components/catalog/PluginCard";
import {
  characterisePendingControls,
  pendingControlsInstruction,
} from "@/components/chat/acknowledgementLabels";
import type { InterpretationEvent } from "@/types/interpretation";
import {
  COMPOSE_CONNECTING_MESSAGE,
  COMPOSE_UNAVAILABLE_MESSAGE,
} from "@/config/composer";
import type { BlobMetadata } from "@/types/api";

/**
 * The sentence appended to the composer after a successful upload. Exported
 * so ChatPanel's freeform ownership fence (elspeth-341a3e2fc4) can append
 * the same sentence into the ORIGINATING session's draft slot when the
 * upload completes after a session switch.
 */
export function uploadedBlobPromptSentence(filename: string): string {
  return `I've uploaded "${filename}"; please use it as the pipeline input.`;
}

/**
 * The subject noun for a pending SET, in the vocabulary of what was actually
 * decided (ux-review 2026-08-13).  The previous wording called every kind
 * "your interpretation of X", which is a category error rather than a loose
 * synonym: a pipeline_decision, an llm_model_choice and an invented_source
 * are all things the COMPOSER chose, and telling an operator they are their
 * own interpretation reassigns authorship at the exact moment they are being
 * asked to attest to them.
 *
 * Prompt-template events carry a machine-facing `user_term`
 * ("llm_prompt_template:<node id>"), so that branch deliberately does not
 * echo the term.  Only vague_term / legacy-null events echo it — those are
 * the ones where the term is genuinely the user's.
 */
function pendingSubjectNoun(
  events: readonly InterpretationEvent[],
  userTerm: string,
): string {
  const count = events.length;
  const kinds = new Set(events.map((event) => event.kind));
  if (kinds.size !== 1) return `${count} composer choices`;
  switch (events[0].kind) {
    case "llm_prompt_template":
      return count === 1
        ? "an LLM-drafted prompt"
        : `${count} LLM-drafted prompts`;
    case "pipeline_decision":
      return count === 1
        ? "a decision the composer made"
        : `${count} decisions the composer made`;
    case "llm_model_choice":
      return count === 1
        ? "the model the composer picked"
        : `${count} models the composer picked`;
    case "invented_source":
      return "source data the composer invented";
    case "vague_term":
    case null:
      return count === 1
        ? `your interpretation of "${userTerm}"`
        : `${count} interpretations of your words`;
    default:
      return unnamedKindNoun(events[0].kind, count);
  }
}

/**
 * Runtime floor for an InterpretationKind added to the union without copy in
 * `pendingSubjectNoun` — without it the switch falls through and the
 * placeholder reads "Reviewing undefined".
 *
 * The `never` parameter keeps the COMPILE-TIME exhaustiveness check that a
 * plain `default` arm would have thrown away: a new kind is a type error at
 * the call site, exactly as `assertNever` does for the card's own switch.
 * The difference is the runtime behaviour, and it is deliberate — this string
 * feeds a textarea placeholder, so throwing would take out the composer input
 * over a missing noun. It degrades to the same neutral wording a mixed set
 * gets, which names no control either.
 */
function unnamedKindNoun(kind: never, count: number): string {
  void kind;
  return count === 1 ? "a composer choice" : `${count} composer choices`;
}

/**
 * Kind-aware, SET-aware pending-review placeholder cue (ux-review
 * 2026-08-13, extends elspeth-0a9f77dd75).
 *
 * Two things this must not do.  It must never name a control the pending
 * card(s) do not render — the card's controls vary by kind, so the control
 * naming is delegated to `characterisePendingControls` /
 * `pendingControlsInstruction`, the same shared rule the injected validation
 * note uses, which returns null for any set it cannot characterise.  And it
 * must characterise the WHOLE pending set rather than the first event: the
 * previous implementation returned on the first event carrying a user_term
 * and spoke in the singular, so a mixed set (a prompt review plus a term
 * review from the same turn — routine) named controls the topmost card does
 * not render, which is the elspeth-0a9f77dd75 defect surviving in the
 * sibling surface.
 *
 * Returns null when no pending event carries a user_term: auto-baked rows
 * (interpretation_source = auto_interpreted_opt_out / no_surfaces) have
 * user_term=null and there is nothing to cue, so the placeholder falls
 * through to the next layer.
 */
export function interpretationCueText(
  events: readonly InterpretationEvent[],
): string | null {
  if (events.length === 0) return null;
  const termed = events.find((event) => event.user_term !== null);
  if (termed === undefined) return null;
  const subject = pendingSubjectNoun(events, termed.user_term ?? "");
  const instruction = pendingControlsInstruction(
    characterisePendingControls(events.map((event) => event.kind)),
    (label) => label,
  );
  const plural = events.length > 1;
  const tail =
    instruction === null
      ? `use the buttons on the review card${plural ? "s" : ""} to continue.`
      : `${instruction} on ${plural ? "each review card" : "the review card"} to continue.`;
  return `Reviewing ${subject} — ${tail}`;
}

interface ChatInputProps {
  onSend: (content: string) => void;
  disabled: boolean;
  onCancel?: () => void;
  inputRef: React.RefObject<HTMLTextAreaElement>;
  onToggleBlobManager?: () => void;
  showBlobManager?: boolean;
  onOpenSecrets?: () => void;
  /** Successful upload metadata, retained by guided mode for exact source binding. */
  onBlobUploaded?: (blob: BlobMetadata) => void;
  /** Upload request lifecycle, used by guided mode to fence async completions. */
  onBlobUploadStarted?: (requestId: string, sessionId: string) => void;
  /** Return false to suppress stale candidate publication and filename insertion. */
  onBlobUploadCompleted?: (
    requestId: string,
    sessionId: string,
    blob: BlobMetadata,
  ) => boolean;
  /** Return false to suppress stale local and blob-store failure publication. */
  onBlobUploadRejected?: (requestId: string, sessionId: string) => boolean;
  onBlobUploadSettled?: (requestId: string, sessionId: string) => void;
  /** Disables only the upload affordance; ordinary text remains independently gated. */
  uploadDisabled?: boolean;
  /** Controlled mode: external value (use with onChange) */
  value?: string;
  /** Controlled mode: callback when value changes */
  onChange?: (value: string) => void;
  /** Optional native textarea maxLength, used by guided chat to mirror backend validation. */
  maxLength?: number;
  /**
   * Optional placeholder override.  Used by the guided-mode chat input
   * (Phase A slice 4) to surface a per-step nudge.  Defaults to the
   * freeform composer wording when absent.
   */
  placeholder?: string;
  /**
   * Tutorial lock: when true the textarea is read-only (the prepopulated
   * prompt cannot be edited) and the source-composition affordances
   * (file upload, blob manager, secrets) are hidden. The learner still
   * presses Send to submit the locked value. Used by the guided tutorial
   * so the worked-example prompt is prepopulated-and-locked — the learner
   * steps through the normal flow but types nothing.
   */
  readOnly?: boolean;
}

// This family's glyphs live in the ui Icon primitive (elspeth-2b88e3ca6d
// retired the local ChatInputIcon after Icon grew folder/upload/key/more with
// geometry copied verbatim). `.chat-input-icon` (chat.css) renders them at
// 20px — CSS width/height beat Icon's 16px attribute defaults, and chat.css
// imports after the ui stylesheet so it also wins over .ui-icon's 1em. The
// "more" glyph's filled-circle rasterisation contract (elspeth-b720e0b932)
// is documented and test-pinned in Icon.tsx/Icon.test.tsx.

export function ChatInput({
  onSend,
  disabled,
  onCancel,
  inputRef,
  onToggleBlobManager,
  showBlobManager,
  onOpenSecrets,
  onBlobUploaded,
  onBlobUploadStarted,
  onBlobUploadCompleted,
  onBlobUploadRejected,
  onBlobUploadSettled,
  uploadDisabled = false,
  value: controlledValue,
  onChange: controlledOnChange,
  maxLength,
  placeholder,
  readOnly = false,
}: ChatInputProps) {
  // Stable id for the keyboard-hint element, wired into the textarea's
  // aria-describedby so screen readers announce "Shift+Enter for new line"
  // alongside the textarea label. useId keeps it stable across renders and
  // unique per ChatInput instance.
  const hintId = useId();
  // Support both controlled and uncontrolled modes
  const [internalText, setInternalText] = useState("");
  const isControlled = controlledValue !== undefined;
  const text = isControlled ? controlledValue : internalText;
  const setText = isControlled
    ? (v: string) => controlledOnChange?.(v)
    : setInternalText;
  const [uploadStatus, setUploadStatus] = useState<string | null>(null);
  const fileInputRef = useRef<HTMLInputElement>(null);
  // Rare-action overflow ("More"): the file-manager toggle and secrets entry
  // fold behind one trigger so the textarea keeps a usable width in the
  // 360px authoring column (elspeth-8fa71e6d15, IA spec Stage 4 #12/#14 —
  // the same overflow idiom as WorkspaceActionBar's "More actions").
  const [moreOpen, setMoreOpen] = useState(false);
  const moreRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!moreOpen) return;
    function handlePointerDown(event: PointerEvent) {
      if (
        event.target instanceof Node &&
        !moreRef.current?.contains(event.target)
      ) {
        setMoreOpen(false);
      }
    }
    document.addEventListener("pointerdown", handlePointerDown);
    return () => document.removeEventListener("pointerdown", handlePointerDown);
  }, [moreOpen]);
  // Track current text in a ref to avoid stale closures during async operations
  const textRef = useRef(text);
  textRef.current = text;
  // Track current setText in a ref to avoid re-registering the prefill listener
  // when setText identity changes (in controlled mode) on each render
  const setTextRef = useRef(setText);
  setTextRef.current = setText;
  const activeSessionId = useSessionStore((s) => s.activeSessionId);
  // Compose-timeout bootstrap gate (elspeth bootstrap race). FALSE until
  // App.checkHealth has applied the backend wall clock; sending before then
  // would schedule a compose-abort timer from the stale default ceiling and
  // could abort a healthy turn before the backend's structured 422. Send is
  // held closed until readiness with a visible reason (dead-button doctrine —
  // a silently inert Send reads as a bug). Both freeform and guided render
  // this input, so this one gate covers both paths.
  const composeTimeoutReady = useSessionStore((s) => s.composeTimeoutReady);
  // Set when the backend is reachable but reported no usable compose timeout,
  // so readiness can never latch. Distinguishes "up but misconfigured" (show a
  // distinct, alerting diagnostic) from the transient boot window (show the
  // soft "Connecting…").
  const composerTimeoutUnavailable = useSessionStore(
    (s) => s.composerTimeoutUnavailable,
  );
  const awaitingComposeTimeout = !composeTimeoutReady;
  // Phase 5a Task 1 — empty-state placeholder primes the user to type data
  // directly into chat (URL / a few rows / a short brief).  Reads two
  // singleton fields on sessionStore (verified: sessionStore.ts:154-155):
  //   - messages: ChatMessage[]                  → message count
  //   - compositionState: CompositionState | null → version (0 when null)
  // Both must read as "empty" to keep the data-priming wording; either
  // signal flipping reverts to the canonical placeholder.
  const messageCount = useSessionStore((s) => s.messages.length);
  const compositionVersion = useSessionStore(
    (s) => s.compositionState?.version ?? 0,
  );
  const uploadBlob = useBlobStore((s) => s.uploadBlob);
  // Phase 5b Task 8 — when a pending interpretation event exists for the
  // active session AND it has a non-null `user_term`, the chat-input
  // placeholder briefly cues the user that the InterpretationReviewTurn
  // widget above is waiting on a decision.  Auto-baked rows
  // (interpretation_source = auto_interpreted_opt_out / no_surfaces) have
  // user_term=null per types/interpretation.ts:101-106 — there is no term
  // to echo, so the cue falls through to the next placeholder layer.
  // Selector returns the finished cue string|null (strings compare by value
  // under zustand's Object.is check) so this component only re-renders when
  // the *displayed cue* changes, not on every pending-map mutation.  The
  // WHOLE pending set is passed to interpretationCueText — it is set-aware,
  // not first-event-wins, so it never names a control some visible card does
  // not render (ux-review 2026-08-13).
  const interpretationCuePlaceholder = useInterpretationEventsStore((s) => {
    if (!activeSessionId) return null;
    const pending = s.pendingBySession[activeSessionId];
    if (!pending) return null;
    return interpretationCueText(Object.values(pending));
  });

  // Listen for prefill events dispatched by InlineChatSourceEntry (catalog
  // Sources tab, Phase 7C). After Phase 7B the PluginCard no longer dispatches
  // this event; after Phase 7C InlineChatSourceEntry is the sole dispatcher.
  // Uses setText (the controlled/uncontrolled abstraction) to set the value,
  // then focuses the textarea via the external inputRef.
  useEffect(() => {
    function handlePrefill(e: Event) {
      const detail = (e as CustomEvent<string>).detail;
      if (typeof detail !== "string") {
        // System-to-system contract violation (the dispatcher always sends a
        // string).  Per CLAUDE.md trust-tier model: internal contract
        // violations crash, not log-and-continue.  This surfaces immediately
        // in dev / tests / DevTools rather than producing a silent no-op
        // that a future contributor wouldn't notice.
        throw new TypeError(
          `[ChatInput] PREFILL_CHAT_INPUT_EVENT: expected string detail, got ${typeof detail}`,
        );
      }
      setTextRef.current(detail);
      // Defer focus + caret placement to a microtask so React flushes the
      // controlled-value re-render first; otherwise focus() runs against a
      // stale textarea value and setSelectionRange uses the wrong length.
      // queueMicrotask (not requestAnimationFrame) keeps the prefill
      // synchronous from the user's perspective — no visible 16ms gap.
      queueMicrotask(() => {
        const ta = inputRef.current;
        if (!ta) return;
        ta.focus();
        const len = detail.length;
        ta.setSelectionRange(len, len);
      });
    }
    window.addEventListener(PREFILL_CHAT_INPUT_EVENT, handlePrefill);
    return () => window.removeEventListener(PREFILL_CHAT_INPUT_EVENT, handlePrefill);
  }, [inputRef]);

  const handleSend = useCallback(() => {
    const trimmed = text.trim();
    // awaitingComposeTimeout gates the Enter-key path too, not just the
    // button: no send may start before a known-good abort ceiling exists.
    if (!trimmed || disabled || awaitingComposeTimeout) return;
    onSend(trimmed);
    // Clear input after send
    if (isControlled) {
      controlledOnChange?.("");
    } else {
      setInternalText("");
    }
  }, [
    text,
    disabled,
    awaitingComposeTimeout,
    onSend,
    isControlled,
    controlledOnChange,
  ]);

  function handleKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === "Enter" && !e.shiftKey) {
      e.preventDefault();
      handleSend();
    }
  }

  async function handleFileSelect(e: ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file || !activeSessionId || uploadDisabled) return;

    const uploadRequestId = crypto.randomUUID();
    const uploadSessionId = activeSessionId;
    const input = e.target;
    setUploadStatus(null);
    onBlobUploadStarted?.(uploadRequestId, uploadSessionId);
    let rejectionAccepted: boolean | null = null;
    try {
      const blob = await uploadBlob(uploadSessionId, file, {
        shouldPublishError: () => {
          rejectionAccepted =
            onBlobUploadRejected?.(uploadRequestId, uploadSessionId) ?? true;
          return rejectionAccepted;
        },
      });
      const accepted =
        onBlobUploadCompleted?.(uploadRequestId, uploadSessionId, blob) ?? true;
      if (!accepted) return;
      onBlobUploaded?.(blob);
      // Use ref to get current text (user may have typed during async upload)
      const currentText = textRef.current;
      const newText =
        currentText +
        (currentText ? "\n" : "") +
        uploadedBlobPromptSentence(blob.filename);
      if (isControlled) {
        controlledOnChange?.(newText);
      } else {
        setInternalText(newText);
      }
    } catch {
      const accepted =
        rejectionAccepted ??
        onBlobUploadRejected?.(uploadRequestId, uploadSessionId) ??
        true;
      if (accepted) {
        // The mapped detail is shown in the blob store / blob manager.
        setUploadStatus("Upload failed. Check the file manager for details.");
      }
    } finally {
      onBlobUploadSettled?.(uploadRequestId, uploadSessionId);
      // Reset the file input so the same file can be re-selected
      input.value = "";
    }
  }

  const canSend = !disabled && !awaitingComposeTimeout && text.trim().length > 0;

  // Read-only (tutorial locked prompt) content is static and multi-line (the
  // worked-example prompt + the sample URLs). A fixed 2-row box clipped it; size
  // the box to the content (capped) so the whole locked prompt is visible
  // without an obscure inner scroll.
  //
  // Editable mode is 3 rows, not 2 (elspeth-244b8ba932). The placeholder is the
  // instruction telling the user what to type, and a placeholder produces no
  // scroll overflow — no scrollbar, no ellipsis — so a placeholder longer than
  // the box is simply cut mid-word with no cue that anything is missing. Every
  // shipped placeholder here is a full sentence: the four guided per-step
  // nudges (ChatPanel.GUIDED_CHAT_PLACEHOLDERS), the empty-state data-priming
  // line, and the pending-interpretation cue. Two rows clipped them in the
  // 360px authoring pane, which is the SHIPPED DEFAULT pane width at the
  // 1280px minimum supported viewport — sizing against the wider 1536px+
  // default would leave the common case still clipped.
  //
  // This is a static row count, not autosizing: nothing here measures
  // scrollHeight, and autosizing would not help anyway because a placeholder
  // contributes no scroll height to measure. The `max-height: min(28dvh,
  // 240px)` ceiling in chat.css still does real work — it bounds the box the
  // user drags with `resize: vertical`.
  const rows = readOnly
    ? Math.min(10, Math.max(3, text.split("\n").length + 1))
    : 3;

  // Phase 5b Task 8 (extends Phase 5a Task 1) — derive the effective
  // placeholder.  Precedence (highest wins):
  //   1. explicit `placeholder` prop (Phase A slice 4 guided-mode nudge)
  //   2. pending-interpretation cue (Phase 5b Task 8 — when an interpretation
  //      review widget is awaiting the user's decision and has a non-null
  //      user_term to echo)
  //   3. empty-state data-priming wording (Phase 5a — no messages, no
  //      composition)
  //   4. canonical "describe the pipeline" wording
  //
  // The pending-interpretation cue sits above empty-state because it
  // describes a *concrete pending decision* the user must address;
  // empty-state is only a generic prime for first-typed-input.  Both
  // continue to sit below an explicit prop so guided-mode per-step nudges
  // (Phase A slice 4) remain authoritative.
  const isEmptyState = messageCount === 0 && compositionVersion === 0;
  const defaultPlaceholder = isEmptyState
    ? "Describe your pipeline, paste a URL, or type a few rows of data to start..."
    : "Describe the pipeline you want to build...";
  // Directionally neutral (elspeth-eba8820005): the review card renders in a
  // different column in the guided workspace, so "above" contradicts the
  // layout at some breakpoints — name the card, not a direction.  The cue
  // itself is built kind-aware in the store selector above
  // (interpretationCueText) so its button names interpolate the card's
  // exported label constants and match the controls the pending card
  // actually renders (elspeth-0a9f77dd75 + ux-review 2026-08-13).
  const effectivePlaceholder =
    placeholder ?? interpretationCuePlaceholder ?? defaultPlaceholder;

  return (
    <div className="chat-input">
      {uploadStatus && (
        <div role="alert" className="chat-input-upload-alert">
          {uploadStatus}
        </div>
      )}
      {/* Bootstrap gate reason. Two states share this slot (both keep Send
          disabled, both hidden while composing so neither collides with Stop):
          - transient boot window: role=status (polite) soft "Connecting…"
            during the (usually sub-second) wait for GET /api/system/status.
          - backend up but no usable compose timeout: role=alert, a distinct
            stuck-state diagnostic so it never reads as a perpetual connect. */}
      {awaitingComposeTimeout &&
        !disabled &&
        (composerTimeoutUnavailable ? (
          // Distinct `key` per branch: force React to unmount the polite status
          // and mount a fresh assertive alert on the Connecting→Unavailable
          // flip, rather than mutate role in place (which SRs may not re-announce).
          <div
            key="unavailable"
            role="alert"
            className="chat-input-composer-unavailable"
          >
            {COMPOSE_UNAVAILABLE_MESSAGE}
          </div>
        ) : (
          <div
            key="connecting"
            role="status"
            className="chat-input-bootstrapping"
          >
            {COMPOSE_CONNECTING_MESSAGE}
          </div>
        ))}
      <div className="chat-input-row" role="group" aria-label="Message composition">
        <textarea
          ref={inputRef}
          data-chat-input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={handleKeyDown}
          placeholder={effectivePlaceholder}
          maxLength={maxLength}
          readOnly={readOnly}
          aria-label="Message input"
          aria-describedby={hintId}
          rows={rows}
          className="chat-input-textarea"
        />

        {/* Forced wrap point for narrow authoring panes: the container query
            in chat.css turns this on so Upload/More drop below the
            textarea/Stop/Send row instead of squeezing the textarea under
            its floor. display:none at ordinary pane widths. */}
        <span className="chat-input-row-break" aria-hidden="true" />

        {/* File upload button — using a visible button that clicks a hidden input */}
        {!readOnly && (
          <>
            <Button
              variant="bare"
              onClick={() => fileInputRef.current?.click()}
              disabled={!activeSessionId || uploadDisabled}
              className="chat-input-icon-btn chat-input-upload-btn"
              title="Upload file"
              aria-label="Upload file"
            >
              <Icon name="upload" className="chat-input-icon" />
            </Button>
            <Input
              ref={fileInputRef}
              type="file"
              // Mirrors the server's closed storage MIME vocabulary
              // (contracts/blobs.py StorageMimeType): data uploads plus the
              // binary document set admitted for Textract inline analysis.
              // The server remains the authority — this only filters the
              // picker.
              accept=".csv,.txt,.json,.jsonl,.png,.jpg,.jpeg,.pdf,text/csv,text/plain,application/json,application/x-jsonlines,application/jsonl,text/jsonl,image/png,image/jpeg,application/pdf"
              onChange={handleFileSelect}
              disabled={!activeSessionId || uploadDisabled}
              style={{ display: "none" }}
              aria-hidden="true"
              tabIndex={-1}
            />
          </>
        )}

        {/* Rare-action overflow: file manager + secrets fold behind one
            trigger (IA spec Stage 4 #12/#14 — Upload stays persistent, it is
            the core "give the composer your data" action). */}
        {!readOnly && (onToggleBlobManager || onOpenSecrets) && (
          <div
            ref={moreRef}
            className="chat-input-more"
            onKeyDown={(event) => {
              if (event.key === "Escape" && moreOpen) {
                event.stopPropagation();
                setMoreOpen(false);
              }
            }}
          >
            <Button
              variant="bare"
              className="chat-input-icon-btn"
              title="More actions"
              aria-label="More actions"
              aria-controls="chat-input-more-panel"
              aria-expanded={moreOpen}
              onClick={() => setMoreOpen((open) => !open)}
            >
              <Icon name="more" className="chat-input-icon" />
            </Button>
            {moreOpen && (
              <div
                id="chat-input-more-panel"
                className="chat-input-more-menu"
                role="group"
                aria-label="More actions"
              >
                {onToggleBlobManager && (
                  <Button
                    compact
                    className="chat-input-more-item"
                    aria-label={
                      showBlobManager ? "Hide file manager" : "Show file manager"
                    }
                    onClick={() => {
                      setMoreOpen(false);
                      onToggleBlobManager();
                    }}
                    iconLeft={<Icon name="folder" className="chat-input-icon" />}
                  >
                    {showBlobManager ? "Hide file manager" : "Show file manager"}
                  </Button>
                )}
                {onOpenSecrets && (
                  <Button
                    compact
                    className="chat-input-more-item"
                    onClick={() => {
                      setMoreOpen(false);
                      onOpenSecrets();
                    }}
                    iconLeft={<Icon name="key" className="chat-input-icon" />}
                  >
                    API keys & secrets
                  </Button>
                )}
              </div>
            )}
          </div>
        )}

        {disabled && onCancel && (
          <Button
            variant="bare"
            onClick={onCancel}
            aria-label="Stop composing"
            className="chat-input-cancel-btn"
          >
            Stop
          </Button>
        )}

        {/* Send button */}
        <Button
          variant="bare"
          onClick={handleSend}
          disabled={!canSend}
          aria-label="Send message"
          aria-keyshortcuts="Enter"
          className="chat-input-send-btn"
        >
          Send
        </Button>

        {/* Hint is the textarea's aria-describedby target — DO NOT mark it
            aria-hidden, that masks it for some screen readers despite the
            describedby reference. Visible to sighted users, announced after
            the textarea's label by AT.
            It renders INSIDE the row as its last flex item
            (elspeth-1b7227936c): at ordinary pane widths its flex-basis:100%
            wraps it onto its own full-width line (same visual position as
            the old sibling arrangement), and under the narrow-pane container
            query it joins the wrapped Upload/More line to fill what was
            ~344px of dead gutter beside a lone 44px Upload button. */}
        <div id={hintId} className="chat-input-hint">
          Shift+Enter for new line
        </div>
      </div>
    </div>
  );
}
