// src/components/chat/MessageBubble.tsx
import { useState, useCallback, useRef, useEffect, useMemo } from "react";
import type {
  ChatMessage,
  CompositionProposal,
  CompositionState,
  InlineSourceSummary,
} from "@/types/api";
import { Button } from "@/components/ui";
import { MarkdownRenderer } from "./MarkdownRenderer";
import { ToolCallCard } from "./ToolCallCard";
import { InlineSourceCreatedTurn } from "./InlineSourceCreatedTurn";

interface MessageBubbleProps {
  message: ChatMessage;
  isComposing?: boolean;
  onRetry?: (messageId: string) => void;
  onFork?: (messageId: string, newContent: string) => void;
  proposalsByToolCallId?: Map<string, CompositionProposal>;
  /**
   * Current composition state, threaded through to ToolCallCard as the
   * "before" side of pending-proposal diffs (elspeth-10f76f9250).
   */
  compositionState?: CompositionState | null;
  staleProposalIds?: string[];
  proposalActionPendingIds?: string[];
  onAcceptProposal?: (proposalId: string) => void;
  onRejectProposal?: (proposalId: string) => void;
  /**
   * Inline source summaries attached to this turn — rendered as a second
   * collapsible group below the tool-calls group, separated by a horizontal
   * ruler. The bubble is the natural home for these because they represent
   * something the agent did *as part of this turn* (created dynamic sources
   * from the user's message). The store currently holds at most one summary
   * per session, but the prop is a list so multiple-source turns work without
   * a future refactor here.
   */
  sourcesCreated?: ReadonlyArray<InlineSourceSummary>;
  onEditInlineSource?: (summary: InlineSourceSummary) => void;
}

export function MessageBubble({
  message,
  isComposing,
  onRetry,
  onFork,
  proposalsByToolCallId,
  compositionState = null,
  staleProposalIds = [],
  proposalActionPendingIds = [],
  onAcceptProposal = () => undefined,
  onRejectProposal = () => undefined,
  sourcesCreated,
  onEditInlineSource,
}: MessageBubbleProps) {
  const isUser = message.role === "user";
  const isSystem = message.role === "system";
  const [toolsExpanded, setToolsExpanded] = useState(false);
  const hasToolCalls = !!(message.tool_calls && message.tool_calls.length > 0);
  const hasSourcesCreated = !!(sourcesCreated && sourcesCreated.length > 0);
  const visibleSegments = useMemo(
    () =>
      message.segments ?? [
        { kind: "text" as const, content: message.content },
      ],
    [message.content, message.segments],
  );
  const [copied, setCopied] = useState(false);
  const [isEditing, setIsEditing] = useState(false);
  const [editContent, setEditContent] = useState(message.content);
  const editRef = useRef<HTMLTextAreaElement>(null);
  const hasProposalToolCall =
    message.tool_calls?.some(
      (tc) => tc.id && proposalsByToolCallId?.has(tc.id),
    ) ?? false;
  const showToolCalls = toolsExpanded || hasProposalToolCall;

  const handleCopy = useCallback(async () => {
    try {
      const plainText = visibleSegments
        .map((segment) =>
          segment.kind === "trusted_system_notice"
            ? `System note: ${segment.content}`
            : segment.content,
        )
        .join("\n\n");
      await navigator.clipboard.writeText(plainText);
      setCopied(true);
      setTimeout(() => setCopied(false), 2000);
    } catch {
      // Clipboard API may fail in insecure contexts
    }
  }, [visibleSegments]);

  const handleEditStart = useCallback(() => {
    setEditContent(message.content);
    setIsEditing(true);
  }, [message.content]);

  const handleEditCancel = useCallback(() => {
    setIsEditing(false);
    setEditContent(message.content);
  }, [message.content]);

  const handleForkSubmit = useCallback(() => {
    if (onFork && editContent.trim()) {
      onFork(message.id, editContent.trim());
      setIsEditing(false);
    }
  }, [onFork, message.id, editContent]);

  useEffect(() => {
    if (isEditing && editRef.current) {
      editRef.current.focus();
      editRef.current.setSelectionRange(
        editRef.current.value.length,
        editRef.current.value.length,
      );
    }
  }, [isEditing]);

  // Author attribution for assistive tech. The bubble distinguishes
  // user/assistant/system VISUALLY (alignment, colour, edge accent), but a
  // screen reader hears a flat run of messages with no idea who said what — on
  // an AI surface, "did ELSPETH or I say this" is load-bearing (WCAG 1.3.1 / AI
  // legibility). An sr-only label, read first, supplies it. (elspeth-f700d8d8a5)
  const authorLabel = isUser ? "You said:" : isSystem ? "System note:" : "ELSPETH said:";

  // System messages: centre-aligned full-width banner, muted colour,
  // italic text. Used for audit markers like "Pipeline reverted to version N."
  if (isSystem) {
    return (
      <div
        className="message-bubble message-bubble--system message-row message-row--system"
      >
        <div
          className="bubble bubble-system"
          role="status"
        >
          <span className="sr-only">{authorLabel}</span>
          <MarkdownRenderer content={message.content} />
        </div>
      </div>
    );
  }

  return (
    <div
      className={`message-bubble message-bubble--${message.role} message-row ${isUser ? "message-row--user" : "message-row--assistant"}`}
    >
      <div
        className={`bubble ${isUser ? "bubble-user" : "bubble-assistant"} message-bubble-content${isUser ? " message-bubble-content--user" : ""}`}
      >
        {/* Author attribution, read first by assistive tech (DOM order). The
            copy button below is an absolutely-positioned overlay, so this stays
            the first thing announced. */}
        <span className="sr-only">{authorLabel}</span>
        {/* Copy button — visible on hover via CSS, always accessible on touch.

            The confirmation is a GLYPH, not the word "Copied!"
            (elspeth-091695b241). This button is `position: absolute; right: 0`
            over the message prose with a `min-width` of
            --size-control-compact (chat.css .bubble-action-overlay), so a
            label wider than that floor can only grow LEFTWARD, over the
            prose. The word rendered ~50-56px
            including padding, so it covered the first line of the text the
            user had just copied — at full opacity (the inline `opacity: 1`
            below) for the whole 2000ms confirmation window, which is long
            enough to read as a rendering glitch rather than a confirmation.

            A single glyph stays well inside that floor, so the control's
            footprint is identical in both states. Nothing is lost for
            assistive tech: the button's aria-label already flips to "Copied to
            clipboard", and an aria-label overrides the element's text content,
            so the word was never the accessible-name channel. U+2713 is the
            product's success mark — the vocabulary audit.css documents and
            AuditReadinessPanel / PipelineValidationSummary render. Dropping
            the word also retires the "Copied!" vs "Copied" voice mismatch
            against MarkdownRenderer's code-block copy affordance. */}
        {!isSystem && (
          <Button
            variant="bare"
            onClick={handleCopy}
            aria-label={copied ? "Copied to clipboard" : "Copy message"}
            className="bubble-copy-btn bubble-action-overlay bubble-action-overlay--copy"
            style={{
              opacity: copied ? 1 : undefined,
            }}
          >
            {copied ? "\u2713" : "\u2398"}
          </Button>
        )}

        {isUser && isEditing ? (
          <div className="message-edit-form">
            <textarea
              ref={editRef}
              value={editContent}
              onChange={(e) => setEditContent(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                  handleForkSubmit();
                } else if (e.key === "Escape") {
                  handleEditCancel();
                }
              }}
              aria-label="Edit message"
              /* Size to the message being edited (mirrors the ChatInput
                 read-only formula): the content is always present at mount,
                 so content-aware rows work here — unlike the composer, where
                 a placeholder has no scroll height and the count must be
                 static. Without this the browser default of 2 rows hid all
                 but the first lines of the message. */
              rows={Math.min(10, Math.max(4, editContent.split("\n").length + 1))}
              className="message-edit-textarea"
            />
            <div className="message-edit-actions">
              <Button
                variant="bare"
                onClick={handleEditCancel}
                className="message-edit-cancel"
              >
                Cancel
              </Button>
              <Button
                variant="bare"
                onClick={handleForkSubmit}
                disabled={!editContent.trim()}
                className="message-edit-fork"
              >
                Fork
              </Button>
            </div>
          </div>
        ) : (
          visibleSegments.map((segment, index) =>
            segment.kind === "trusted_system_notice" ? (
              <div
                key={`trusted-system-notice-${index}`}
                className="trusted-system-notice"
                role="status"
              >
                <span className="sr-only">System note:</span>
                <MarkdownRenderer content={segment.content} />
              </div>
            ) : isUser ? (
              <span key={`message-text-${index}`}>{segment.content}</span>
            ) : (
              <MarkdownRenderer
                key={`message-text-${index}`}
                content={segment.content}
              />
            ),
          )
        )}

        {/* Edit/fork button — user messages only, not pending/failed */}
        {isUser && !isEditing && !message.local_status && onFork && (
          <Button
            variant="bare"
            onClick={handleEditStart}
            aria-label="Edit and fork from this message"
            className="bubble-edit-btn bubble-action-overlay bubble-action-overlay--edit"
          >
            &#9998;
          </Button>
        )}

        {isUser && message.local_status === "failed" && onRetry && (
          <div className="message-failed-row">
            <span className="message-failed-text">
              {message.local_error ?? "Failed to send message. Please try again."}
            </span>
            {/* S1: ``policy_blocked`` is permanent by construction — a
                deployment policy refused the pipeline (see the F13-D guided
                precedent in sessionStore.ts) — so keep the failed text but
                never render a retry invitation for it. */}
            {message.local_failure_code !== "policy_blocked" && (
              <Button
                variant="bare"
                onClick={() => onRetry(message.id)}
                className="message-retry-btn"
                // Retry is a compose entry point: the store admission gate
                // (elspeth-3f38ebb1b5) refuses a second compose while one is
                // in flight, and the affordance must say so.
                disabled={isComposing}
              >
                Retry
              </Button>
            )}
          </div>
        )}

        {isUser && message.local_status === "pending" && !isComposing && (
          <div className="message-pending">
            Sending...
          </div>
        )}

        {/* Tool calls section (assistant messages only) */}
        {message.tool_calls && message.tool_calls.length > 0 && (
          <div className="message-tools">
            <Button
              variant="bare"
              onClick={() => setToolsExpanded(!toolsExpanded)}
              aria-expanded={showToolCalls}
              aria-label={`Tool calls (${message.tool_calls.length})`}
              className="message-tools-toggle"
            >
              {showToolCalls ? "\u25BC" : "\u25B6"} Tool calls (
              {message.tool_calls.length})
            </Button>
            {showToolCalls && (
              <div className="message-tools-list">
                {message.tool_calls.map((tc, i) => (
                  <ToolCallCard
                    key={tc.id ?? i}
                    toolCall={tc}
                    currentState={compositionState}
                    proposal={
                      tc.id
                        ? proposalsByToolCallId?.get(tc.id) ?? null
                        : null
                    }
                    isStale={
                      tc.id
                        ? staleProposalIds.includes(
                            proposalsByToolCallId?.get(tc.id)?.id ?? "",
                          )
                        : false
                    }
                    isBusy={
                      tc.id
                        ? proposalActionPendingIds.includes(
                            proposalsByToolCallId?.get(tc.id)?.id ?? "",
                          )
                        : false
                    }
                    onAccept={onAcceptProposal}
                    onReject={onRejectProposal}
                  />
                ))}
              </div>
            )}
          </div>
        )}

        {/* Horizontal ruler between Tool calls and Sources created — only
            rendered when both groups exist. Without this guard the bubble
            would carry a stray separator when there are zero tool calls but
            one source-created event (e.g. a hello-world first message that
            creates a dynamic source without invoking any tools). */}
        {hasToolCalls && hasSourcesCreated && (
          <hr className="message-group-separator" aria-hidden="true" />
        )}

        {/* Sources created section (assistant messages only).
            Deliberately NOT a collapsible disclosure (unlike Tool calls
            above). Source creation is a notification of an action that
            just got attached to the composition — the user needs to see
            it to decide whether to amend or proceed. Burying it behind
            a twisty would defer an actionable moment behind a click,
            which is the opposite of "hey, this happened, you need to
            know". The visual heading uses the same styling as the tool-
            calls toggle (.message-tools-toggle) so the two groups still
            read as siblings in the bubble, but the heading is a static
            <div> rather than a button — no aria-expanded, nothing to
            toggle. The inner InlineSourceCreatedTurn widget still has
            its own audit-info <details> disclosure for the SHA-256 hash;
            that nested twisty shows the cryptographic detail on demand
            without hiding the notification itself. */}
        {hasSourcesCreated && (
          <div className="message-sources-created">
            <div className="message-tools-toggle message-sources-created-heading">
              Sources ({sourcesCreated!.length})
            </div>
            <div className="message-sources-created-list">
              {sourcesCreated!.map((summary) => (
                <InlineSourceCreatedTurn
                  key={summary.blobId}
                  summary={summary}
                  onEdit={onEditInlineSource ?? (() => undefined)}
                />
              ))}
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
