// src/components/chat/ChatPanel.tsx
import {
  useEffect,
  useLayoutEffect,
  useMemo,
  useRef,
  useCallback,
  useState,
  type SetStateAction,
} from "react";
import { Button } from "@/components/ui";
import { useSessionStore } from "@/stores/sessionStore";
import { useBlobStore } from "@/stores/blobStore";
import {
  deriveInlineSourceRowCount,
  projectInlineSourceSummary,
  useInlineSourceStore,
} from "@/stores/inlineSourceStore";
import { useComposer } from "@/hooks/useComposer";
import { FOCUSABLE_SELECTOR } from "@/hooks/useFocusTrap";
import {
  getBlobMetadata,
  previewBlobContent,
  toInlineSourceProvenance,
} from "@/api/client";
import { MessageBubble } from "./MessageBubble";
import { groupIntoTurns, turnRepresentativeMessage, type ChatTurn } from "./turns";
import { ComposingIndicator } from "./ComposingIndicator";
import { AuthorityChip } from "./AuthorityChip";
import { ChatInput, uploadedBlobPromptSentence } from "./ChatInput";
import { FreeformIntroduction } from "./FreeformIntroduction";
import { BlobManager } from "@/components/blobs/BlobManager";
import { CompletionSummary } from "./guided/CompletionSummary";
import { ModeSwitchButton } from "./guided/ModeSwitchButton";
import { PendingProposalsBanner } from "./PendingProposalsBanner";
import { GuidedChatHistory } from "./guided/GuidedChatHistory";
import { GuidedPendingStrip } from "./guided/GuidedPendingStrip";
import { GUIDED_EXPLAIN_MESSAGE } from "./guided/explainPrompt";
import { GUIDED_STEP_LABELS } from "./guided/stepLabels";
import {
  useCompletionOutcome,
  type CompletionOutcome,
} from "./completionOutcome";
import { GuidedDecisionPendingIndicator } from "./guided/GuidedDecisionPendingIndicator";
import { GuidedTurn } from "./guided/GuidedTurn";
import { isGuidedBuildActive } from "./guided/guidedBuildActive";
import { latestAssistantRationale } from "./guided/guidedRationale";
import { clientWireBlockerMessages, humaniseValidationMessage, makePhraseFor } from "@/lib/validationHumaniser";
import {
  AcknowledgementLiveRegion,
  AcknowledgementStack,
  useHasPendingGuidedInterpretations,
  usePendingAcknowledgements,
} from "./AcknowledgementStack";
import { acknowledgementCardTitle } from "./AcknowledgementCard";
import { humaniseStepLabel } from "./interpretationStepLabel";
import {
  COMPOSE_CONNECTING_MESSAGE,
  COMPOSE_UNAVAILABLE_MESSAGE,
  COMPOSE_USER_CANCEL_ABORT_REASON,
  runComposeWithTimeout,
} from "@/config/composer";
import type { WireBlockerLink } from "./guided/WireStageTurn";
import { InlineSourceCreatedTurn } from "./InlineSourceCreatedTurn";
import { InlineSourceDisambiguationTurn } from "./InlineSourceDisambiguationTurn";
import { InlineSourceFallbackPrompt } from "./InlineSourceFallbackPrompt";
import { sortedSourceEntries } from "@/utils/compositionState";
import type {
  BlobMetadata,
  ChatMessage,
  CompositionProposal,
  CompositionState,
  InlineSourceSummary,
} from "@/types/api";
import {
  GUIDED_CHAT_MESSAGE_MAX_LENGTH,
  type ChatTurn as GuidedChatTurn,
  type GuidedSourceBlobCandidate,
  type GuidedStep,
  type GuidedRevisionMode,
} from "@/types/guided";

function isTerminalComposerPhase(
  phase: string | null | undefined,
): boolean {
  return phase === "complete" || phase === "failed" || phase === "cancelled";
}

function objectHasOnlyKeys(
  value: Record<string, unknown>,
  allowed: ReadonlySet<string>,
): boolean {
  return Object.keys(value).every((key) => allowed.has(key));
}

function hasOwn(value: Record<string, unknown>, key: string): boolean {
  return Object.prototype.hasOwnProperty.call(value, key);
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isAbsentOrNull(value: Record<string, unknown>, key: string): boolean {
  return !hasOwn(value, key) || value[key] === null;
}

function isEmptyRedactedOptions(value: unknown): boolean {
  if (value === null || value === undefined) return true;
  if (typeof value === "string") return value.trim() === "{}";
  if (isRecord(value)) return Object.keys(value).length === 0;
  return false;
}

const DEFAULT_PIPELINE_METADATA_NAME = "Untitled Pipeline";

const TUTORIAL_STEP_2_COMPOSING_SUBSTEPS = [
  "Read output request",
  "Choose sink shape",
  "Prepare JSON file",
] as const;

function tutorialStep2ActiveSubstep(
  phase: string | null | undefined,
): number {
  if (phase === null || phase === undefined || phase === "starting") {
    return 0;
  }
  // calling_model and using_tools share substep 1 ("Choose sink shape"):
  // the guided sink resolver (maybe_resolve_step_2_sink_chat) re-emits
  // calling_model before EVERY provider round, including the round after
  // a discovery tool call. Mapping calling_model to substep 0 (as this
  // function used to) made the indicator jump backward mid-compose —
  // 0 -> 1 (using_tools) -> 0 (next calling_model) -> 2 (complete) — for
  // a ticket titled "never advances", a visible regression back to 0 is
  // worse than the original bug. Folding both phases into substep 1 is
  // monotonic regardless of how many discovery rounds run, and — the
  // common case for the tutorial's one-line "Save ... to a JSON file"
  // prompt — still lights up substep 1 even when the model resolves in a
  // single shot with no discovery tool call at all (elspeth-a8eeebb3aa).
  if (phase === "calling_model" || phase === "using_tools") {
    return 1;
  }
  return 2;
}

/**
 * Best-effort row-count from CSV-like text content.
 *
 * Returns `null` when the mime type is not CSV-shaped — we'd rather be honest
 * about not knowing than infer a misleading number (see CLAUDE.md "fabrication
 * decision test"). For text/csv we count newline-separated rows after
 * trimming, subtracting one for the header row when there is content.
 *
 * MIME normalisation: the input may be parameterised (e.g.
 * "text/csv; charset=utf-8" — a perfectly valid value the server may
 * record verbatim from the upload's Content-Type header).  We split on
 * `;` and lowercase before the comparison so parameterised CSVs are not
 * silently classified as "unknown row count".  RFC 7231 §3.1.1.1 reserves
 * `;` as the parameter separator; a literal `;` cannot appear inside the
 * type/subtype tokens.
 *
 * Exported for the ChatPanel test seam — the integration tests stub the blob
 * fetchers and feed text directly into this projector.
 */
export function deriveRowCount(
  mimeType: string,
  text: string,
): number | null {
  return deriveInlineSourceRowCount(mimeType, text);
}

// ── Inline-source disambiguation heuristic (Phase 5a Task 4) ─────────────────
//
// Detect whether a pending composer proposal is BOTH (a) an inline-blob
// source-creation proposal AND (b) one whose row-count interpretation
// looks ambiguous enough to warrant explicit confirmation from the user.
//
// Heuristic v1 (intentionally narrow — false negatives are recoverable
// because the standard PendingProposalsBanner still routes the proposal;
// false positives DO produce a disruptive widget where a banner would
// have sufficed):
//
//   * The proposal's `tool_name` must be "set_pipeline" (the only tool
//     surface that carries an inline_blob source today; the inline-blob
//     create path lives inside set_pipeline.source.inline_blob — see
//     src/elspeth/web/composer/redaction.py:_InlineBlobModel).
//   * The proposal's `arguments_redacted_json` must contain the production
//     set_pipeline scaffold — `source`, `nodes`, `edges`, `outputs` — but
//     `nodes` / `edges` / `outputs` must be empty arrays and `source` must be
//     an inline blob with only the required source plugin/routing label plus
//     backend-redaction defaults. `sources`, `metadata`, `blob_id`,
//     `on_validation_failure`, and `inline_blob.description` must be absent or
//     null, and `options` must be absent or the redacted empty object. Because
//     `set_pipeline` is an atomic full-state replacement, any non-default graph,
//     output, metadata, source-option, or routing change stays on the standard
//     approval banner with the full context visible.
//   * The proposal's `summary` must contain a recognised
//     row-count-ambiguity phrase — currently "I read" or "interpreted as".
//     A Phase 5b refactor will replace this with a structured annotation
//     emitted by the composer pipeline; until then the heuristic is the
//     contract.
//
// The canonical demo prompt (create a data source with government-page URLs
// and explicit scrape-contact fields) MUST NOT trip this heuristic —
// that proposal's summary describes LLM-generated source rows, not an
// interpretation of the user's input. The
// `isAmbiguousInlineProposal` unit tests pin this behaviour.
//
// Exported for the ChatPanel test seam.
export function isAmbiguousInlineProposal(
  proposal: CompositionProposal,
): boolean {
  if (proposal.tool_name !== "set_pipeline") return false;

  // Walk the arguments tree without coercion. The redaction layer
  // preserves the inline_blob marker even when the content has been
  // summarised, so a structural check is sufficient and does NOT need
  // to peek at the (possibly redacted) content string. Because set_pipeline
  // is a full-state replacement, the widget is allowed only for the narrow
  // production source-only scaffold: source plus empty nodes/edges/outputs.
  // Any non-empty graph/output/metadata/source-option change stays on the
  // standard proposal banner with the full approval context visible.
  const args = proposal.arguments_redacted_json;
  if (
    !objectHasOnlyKeys(
      args,
      new Set(["source", "sources", "nodes", "edges", "outputs", "metadata"]),
    )
  ) {
    return false;
  }
  if (!isAbsentOrNull(args, "sources") || !isAbsentOrNull(args, "metadata")) {
    return false;
  }
  if (
    !hasOwn(args, "nodes") ||
    !hasOwn(args, "edges") ||
    !hasOwn(args, "outputs") ||
    !Array.isArray(args["nodes"]) ||
    !Array.isArray(args["edges"]) ||
    !Array.isArray(args["outputs"]) ||
    args["nodes"].length !== 0 ||
    args["edges"].length !== 0 ||
    args["outputs"].length !== 0
  ) {
    return false;
  }

  const source = args["source"];
  if (!isRecord(source)) return false;
  const sourceRecord = source;
  if (
    !objectHasOnlyKeys(
      sourceRecord,
      new Set([
        "plugin",
        "on_success",
        "blob_id",
        "options",
        "on_validation_failure",
        "inline_blob",
      ]),
    )
  ) {
    return false;
  }
  if (
    !isAbsentOrNull(sourceRecord, "blob_id") ||
    !isAbsentOrNull(sourceRecord, "on_validation_failure") ||
    (hasOwn(sourceRecord, "options") &&
      !isEmptyRedactedOptions(sourceRecord["options"]))
  ) {
    return false;
  }
  if (typeof sourceRecord["plugin"] !== "string" || sourceRecord["plugin"] === "") {
    return false;
  }
  if (
    typeof sourceRecord["on_success"] !== "string" ||
    sourceRecord["on_success"] === ""
  ) {
    return false;
  }

  const inlineBlob = sourceRecord["inline_blob"];
  if (!isRecord(inlineBlob)) return false;
  if (
    !objectHasOnlyKeys(
      inlineBlob,
      new Set(["filename", "mime_type", "content", "description"]),
    )
  ) {
    return false;
  }
  if (!isAbsentOrNull(inlineBlob, "description")) return false;

  const summary = proposal.summary;
  // Case-insensitive substring match. Two recognised ambiguity phrases
  // — composer narration that explicitly frames "I parsed your input
  // as N rows" or "interpreted as N items". The canonical demo
  // proposal does NOT use either phrase (it describes generation, not
  // interpretation).
  const lowered = summary.toLowerCase();
  return lowered.includes("i read") || lowered.includes("interpreted as");
}

export function hasExistingCompositionContent(
  state: CompositionState | null | undefined,
): boolean {
  const metadataName = state?.metadata.name?.trim() ?? "";
  const metadataDescription = state?.metadata.description?.trim() ?? "";
  return (
    state !== null &&
    state !== undefined &&
    (Object.keys(state.sources).length > 0 ||
      state.nodes.length > 0 ||
      state.edges.length > 0 ||
      state.outputs.length > 0 ||
      (metadataName.length > 0 &&
        metadataName !== DEFAULT_PIPELINE_METADATA_NAME) ||
      metadataDescription.length > 0)
  );
}

export function hasSafeInlineSourceDisambiguationBase(
  proposal: CompositionProposal,
  state: CompositionState | null | undefined,
): boolean {
  if (hasExistingCompositionContent(state)) return false;
  if (state === null || state === undefined) return proposal.base_state_id === null;
  return true;
}

// ── Originating user-message resolution ──────────────────────────────────────
//
// The F-10 / F-11 re-fire guards key on the user message ID that
// triggered the assistant's tool-call. The proposal itself only
// carries `tool_call_id`; we recover the user message by walking the
// chat history: find the assistant message bearing that tool call,
// then walk backwards to the nearest preceding user message.
//
// Returns null when:
//   * No assistant message has a tool call with the given id (proposal
//     orphaned from its message — should not happen in production but
//     we surface as null rather than crash).
//   * The assistant message exists but no user message precedes it
//     (also shouldn't happen — the composer always responds to a user
//     turn — but null is the honest signal).
//
// Callers that need a non-null message ID (e.g., to populate the F-10
// guard) MUST handle null by falling back to the standard banner —
// firing the F-10 guard with a synthesised ID would corrupt the
// guard's data model.
export function findOriginatingMessageId(
  messages: ReadonlyArray<ChatMessage>,
  toolCallId: string,
): string | null {
  const assistantIndex = messages.findIndex((m) =>
    m.tool_calls?.some((tc) => tc.id === toolCallId) ?? false,
  );
  if (assistantIndex < 0) return null;
  for (let i = assistantIndex - 1; i >= 0; i -= 1) {
    if (messages[i].role === "user") return messages[i].id;
  }
  return null;
}

/**
 * Extract the proposed-rows list from an inline-blob proposal's
 * arguments tree. The redaction layer summarises the content string
 * itself, so the only durable row-count signal in the proposal is
 * the structural shape of any inline-blob fields the LLM populated
 * alongside the (now-redacted) content — chiefly the JSON schema's
 * own row metadata. Today the inline_blob redacted shape exposes
 * `filename` / `mime_type` but NOT a parsed row list — see
 * `_InlineBlobModel` in redaction.py.
 *
 * For Phase 5a Task 4 we surface the user's original input as the
 * source of truth for the row list (split on common delimiters) so
 * the widget has something concrete to show. This is a presentation
 * concern only — the authoritative row data lives in the eventual
 * inline_blob.content on the server; the displayed list is a parse
 * preview the user is being asked to confirm.
 *
 * Exported for the ChatPanel test seam.
 */
export function parseProposedRowsFromUserInput(
  userInput: string,
): ReadonlyArray<string> {
  // Strip a leading prose preamble like "check these URLs:" — the
  // rows-of-interest are typically after the first ":" when present.
  const afterColon = userInput.includes(":")
    ? userInput.slice(userInput.indexOf(":") + 1)
    : userInput;
  // Split on commas OR newlines; trim each fragment; drop empties.
  // We intentionally use a permissive split rather than try to do
  // CSV-grade quoting — the user has the "Edit the rows" affordance
  // for cases where the heuristic guesses wrong.
  return afterColon
    .split(/[,\n]/)
    .map((s) => s.trim())
    .filter((s) => s.length > 0);
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

function readBlobRef(state: CompositionState | null): string | null {
  if (state === null) return null;
  for (const [, source] of sortedSourceEntries(state)) {
    const ref = readSourceBlobRef(source);
    if (ref !== null) return ref;
  }
  return null;
}

function isInlineSourceBlob(metadata: BlobMetadata): boolean {
  return (
    metadata.created_by === "assistant" &&
    metadata.created_from_message_id !== null
  );
}

/**
 * Per-step placeholder text for the chat input in guided mode (Phase A slice 4).
 *
 * The wording frames what's *useful* to ask at each wizard step.  This is a
 * UX nudge, not a server-enforced scope — the backend still validates
 * step_index against the live session.step and the per-step skill briefing
 * shapes what the LLM will engage with.  Mirrors the playbook fragments in
 * src/elspeth/web/composer/guided/skills/step_*.md.
 *
 * CLOSED LIST — must cover every GuidedStep member.  Adding a new step
 * member without extending this map produces a TypeScript exhaustiveness
 * error at the lookup site (see assertion in the lookup below).
 */
const GUIDED_CHAT_PLACEHOLDERS: Record<GuidedStep, string> = {
  step_1_source:
    "Describe the source you have — e.g. a CSV, a store query, or pages to scrape…",
  step_2_sink:
    "Describe the output you want — the shape and fields the pipeline should produce…",
  step_3_transforms:
    "Describe what each row should become, or how to fix the proposed transforms…",
  // Names the real next action instead of circularly pointing at a possibly
  // disabled button (elspeth-3b35abf148 variant 1): at the wire stage the
  // unblock path is the acknowledgement cards + the Confirm wiring button on
  // the current decision card. "Card", not "panel": the copy is shared by
  // both layouts, and the tutorial workspace no longer has a decision panel
  // beside the composer — the decision is a card in the conversation column.
  // Length budget (Wave D2 measured handoff): a placeholder produces no
  // scroll overflow, so anything past the 3-row box at the 360px pane is cut
  // silently. The previous 93-char wording rendered 4 lines with the last
  // clipped; every fitting sibling placeholder is ≤80 chars, so this stays
  // ≤80. "Clear" is the stack's own doctrine ("clear it to proceed",
  // AcknowledgementStack.tsx header).
  step_4_wire:
    "Clear pending acknowledgements, then press Confirm wiring on the decision card.",
};

interface ChatPanelProps {
  onOpenSecrets?: () => void;
  // Concern B (LLM-primary spec §"Frontend"): a TUTORIAL session must never
  // reach a freeform surface. This client-only flag is passed truthy ONLY by
  // TutorialGuidedShell. It is deliberately NOT a wire/profile field — there
  // is no tutorial discriminator on the wire (ground truth Q2/Q4), and
  // inferring tutorial from profile booleans is fragile. When true it (i)
  // suppresses ExitToFreeformButton, (ii) suppresses CompletionSummary's
  // "Open freeform editor" button, and (iii) redirects the discriminator's
  // freeform fall-through to a guided placeholder (Task 3).
  isTutorial?: boolean;
  /**
   * Tutorial locked prompts, PER GUIDED STAGE. When set, the guided "Describe
   * what you want" chat input is prepopulated with the CURRENT phase's prompt
   * (keyed by `guidedSession.step`) and locked read-only, so the tutorial
   * learner steps through the normal staged flow without typing — each phase
   * gets only its stage's intent. Supplied by TutorialGuidedShell (per-stage
   * worked-example prompts; source carries the resolved synthetic URLs). Steps
   * with no entry (wire) is confirm-only. Absent for a normal session
   * (the input behaves as the editable freeform-intent box).
   */
  lockedChatPrompt?: Partial<Record<GuidedStep, string>>;
}

interface GuidedSourceBlobCandidateSet {
  sessionId: string;
  turnToken: string;
  candidates: readonly GuidedSourceBlobCandidate[];
  /**
   * Sticky consent latch for this exact turn. Once the user has needed to
   * distinguish files, filtering must not silently turn the remaining file
   * back into an implicit choice.
   */
  requiresExplicitChoice: boolean;
}

interface GuidedUploadFence {
  sessionId: string;
  activationEpoch: number;
  step: GuidedStep | null;
  turnToken: string | null;
  isSourceSingleSelect: boolean;
  /** Plugin of the live Step-1 source schema_form turn, else null — an upload
   * completing against this fence prefills that form's local path draft
   * (elspeth-c70909c13a) instead of injecting the chat message. */
  sourceSchemaFormPlugin: string | null;
}

interface GuidedUploadContext {
  activeSessionId: string | null;
  activationEpoch: number;
  step: GuidedStep | null;
  turnToken: string | null;
  isSourceSingleSelect: boolean;
  sourceSchemaFormPlugin: string | null;
}

/** A late upload binds to a source form only when the filename plausibly
 * matches the already-chosen plugin — the client-side mirror of the backend's
 * `_inspection_matches_source_plugin` (which sniffs content; the client only
 * has the name). A mismatch falls back to today's behavior untouched. */
const SOURCE_PLUGIN_UPLOAD_EXTENSIONS: Record<string, readonly string[]> = {
  csv: [".csv"],
  json: [".json", ".jsonl"],
  text: [".txt"],
};

function uploadMatchesSourcePlugin(plugin: string, filename: string): boolean {
  const extensions = SOURCE_PLUGIN_UPLOAD_EXTENSIONS[plugin];
  if (extensions === undefined) return false;
  const lower = filename.toLowerCase();
  return extensions.some((extension) => lower.endsWith(extension));
}

const CANONICAL_BLOB_UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;

/**
 * Main chat panel combining the message list, composing indicator, and input.
 *
 * Auto-scrolls to the bottom on new messages unless the user has scrolled up.
 * Focus returns to the ChatInput textarea after the assistant response arrives.
 */
export function ChatPanel({
  onOpenSecrets,
  isTutorial,
  lockedChatPrompt,
}: ChatPanelProps) {
  const messages = useSessionStore((s) => s.messages);
  // Project audit-grade message rows onto user-visible turns. One bubble per
  // turn — see ./turns.ts for the grouping rules. Memoised on the messages
  // reference because the store updates the array on append, not in place.
  const chatTurns = useMemo(() => groupIntoTurns(messages), [messages]);
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
  const blobs = useBlobStore((s) => s.blobs);
  const blobActivationEpoch = useBlobStore((s) => s.activationEpoch);
  const compositionState = useSessionStore((s) => s.compositionState);
  const compositionProposals = useSessionStore((s) => s.compositionProposals);
  const staleProposalIds = useSessionStore((s) => s.staleProposalIds);
  const proposalActionPendingIds = useSessionStore(
    (s) => s.proposalActionPendingIds,
  );
  const acceptProposal = useSessionStore((s) => s.acceptProposal);
  const rejectProposal = useSessionStore((s) => s.rejectProposal);
  const applyResolvedInterpretation = useSessionStore(
    (s) => s.applyResolvedInterpretation,
  );
  const composerProgress = useSessionStore((s) => s.composerProgress);
  const clearError = useSessionStore((s) => s.clearError);
  const forkFromMessage = useSessionStore((s) => s.forkFromMessage);
  // Honest completion labels (elspeth-bf9c296ee5), derived from the run
  // gate's own signals rather than the generic terminal phase. Two axes of
  // the same derivation (hoisted here per Rules of Hooks):
  //   - freeform terminal badge: pipelineMutated is the per-turn verdict the
  //     compose success branches persist; `?? true` keeps the legacy
  //     "Updated"-family claim when the verdict is unknowable (e.g. a
  //     terminal progress snapshot restored by polling after a reload);
  //   - guided completion stepper: a completed guided terminal always
  //     carries a composed pipeline, so its axis is fixed `true`.
  const lastComposeChangedPipeline = useSessionStore(
    (s) => s.lastComposeChangedPipeline,
  );
  const freeformCompletionOutcome = useCompletionOutcome(
    activeSessionId,
    lastComposeChangedPipeline ?? true,
  );
  const guidedCompletionOutcome = useCompletionOutcome(activeSessionId, true);
  // Guided-mode discriminator state.  Selectors are hoisted here (not inside a
  // branch) to comply with React's Rules of Hooks; the discriminator early
  // returns below decide which surface to render based on these values.
  const guidedSession = useSessionStore((s) => s.guidedSession);
  const guidedNextTurn = useSessionStore((s) => s.guidedNextTurn);
  const guidedProposalReview = useSessionStore((s) => s.guidedProposalReview);
  const respondGuided = useSessionStore((s) => s.respondGuided);
  const chatGuided = useSessionStore((s) => s.chatGuided);
  const startGuided = useSessionStore((s) => s.startGuided);
  const guidedChatPending = useSessionStore((s) => s.guidedChatPending);
  const guidedResponsePending = useSessionStore((s) => s.guidedResponsePending);
  // Bootstrap-race gate (single source of truth): guided sends read the same
  // readiness the shared runComposeWithTimeout enforces for freeform.
  const composeTimeoutReady = useSessionStore((s) => s.composeTimeoutReady);
  // Stuck state (backend up but reported no compose timeout): drives the
  // Explain button's disabled reason so it matches the main Send instead of
  // saying "Connecting…" forever.
  const composerTimeoutUnavailable = useSessionStore(
    (s) => s.composerTimeoutUnavailable,
  );
  const guidedSelfHealNotice = useSessionStore((s) => s.guidedSelfHealNotice);
  const isProposalRevisionComposer =
    guidedSession?.step === "step_3_transforms" &&
    guidedNextTurn?.type === "propose_pipeline";
  const proposalRevisionIdentity = isProposalRevisionComposer
    ? `${activeSessionId ?? ""}:${guidedNextTurn.payload.proposal_id}:${guidedNextTurn.payload.draft_hash}`
    : null;
  const [guidedRevisionSelection, setGuidedRevisionSelection] = useState<{
    identity: string | null;
    mode: GuidedRevisionMode;
  }>({ identity: proposalRevisionIdentity, mode: "amend" });
  const guidedRevisionMode =
    guidedRevisionSelection.identity === proposalRevisionIdentity
      ? guidedRevisionSelection.mode
      : "amend";
  useEffect(() => {
    setGuidedRevisionSelection({
      identity: proposalRevisionIdentity,
      mode: "amend",
    });
  }, [proposalRevisionIdentity]);
  // Whether the CURRENT chat has any work — gates the mode-switch confirmation
  // (ModeSwitchButton). Freeform work = messages or a non-empty composition;
  // guided work = any chat turns or completed steps. Switching is
  // non-destructive (the session retains both modes' state either way), so this
  // only decides whether a stray click needs a confirm vs a single click.
  const currentChatHasWork =
    messages.length > 0 ||
    hasExistingCompositionContent(compositionState) ||
    (guidedSession !== null &&
      (guidedSession.chat_history.length > 0 ||
        guidedSession.history.length > 0));
  const guidedSwitchDisabledReason = undefined;
  // D12 / P3.6: block guided advancement while any pending user_approved
  // interpretation card remains in the store. Hook is unconditional (called at
  // the component top, not inside the conditional guided return); the empty
  // session id is safe — pendingBySession[""] is undefined, so it returns false.
  const hasPendingGuidedInterpretations = useHasPendingGuidedInterpretations(
    activeSessionId ?? "",
  );
  // Named blockers for the wire-stage confirm (elspeth-3b35abf148 variant 1):
  // the SAME pending cards the AcknowledgementStack renders (same order, same
  // titles), projected to jump links WireStageTurn renders under the disabled
  // Confirm-wiring button. Unconditional hooks — see the note above.
  const pendingAcknowledgementEvents = usePendingAcknowledgements(
    activeSessionId ?? "",
  );
  const wirePendingAcknowledgements = useMemo<WireBlockerLink[]>(
    () =>
      pendingAcknowledgementEvents.map((event) => ({
        id: event.id,
        label: acknowledgementCardTitle(
          event,
          humaniseStepLabel(compositionState, event.affected_node_id),
        ),
      })),
    [pendingAcknowledgementEvents, compositionState],
  );
  // Client-known validation blockers (elspeth-3b35abf148 variant 3, client
  // side): the persisted composition carries its Stage-1 errors; a non-empty
  // list means a confirm would be rejected server-side, so WireStageTurn
  // disables the button and names the issues instead of offering a dead click.
  // The guided deferred-commit placeholder status is excluded: pre-commit
  // guided states are empty-by-design until Confirm wiring commits the
  // proposal, so that status is resolved by the confirm itself and gating on
  // it deadlocks guided authoring (see clientWireBlockerMessages).
  // Messages route through the same humaniser the validation summary uses —
  // an engineer-grade contract dump must not land verbatim in the blockers
  // panel either (same error-rendering discipline).
  const wireValidationIssues = useMemo<string[]>(() => {
    const raw = clientWireBlockerMessages(compositionState?.validation_errors ?? []);
    if (raw.length === 0) return raw;
    const phraseFor = makePhraseFor(compositionState);
    return raw.map(
      (message) => humaniseValidationMessage(message, phraseFor).headline,
    );
  }, [compositionState]);
  // Guided chat abort plumbing (elspeth-fb4464cdf0): the same
  // AbortController + client-timeout treatment freeform gets from
  // useComposer.runWithTimeout, scoped to the guided step composer. Stored on
  // a ref so Stop can abort the in-flight fetch; the store's chatGuided catch
  // maps the abort reason to the cancelled/timeout copy and resets
  // guidedChatPending so the turn can be retried.
  const guidedChatControllerRef = useRef<AbortController | null>(null);
  const sendGuidedChat = useCallback(
    (content: string, revisionMode?: GuidedRevisionMode, retryTurnToken?: string) =>
      // Same shared primitive freeform's useComposer.runWithTimeout uses: one
      // timer + readiness guard for both paths. Until the backend wall clock
      // has landed at boot the guided send does not run (no request, no
      // timer) — the guided input is disabled until readiness.
      runComposeWithTimeout(guidedChatControllerRef, composeTimeoutReady, (signal) =>
        retryTurnToken !== undefined
          ? chatGuided(content, signal, undefined, retryTurnToken)
          : revisionMode === undefined
            ? chatGuided(content, signal)
            : chatGuided(content, signal, revisionMode),
      ),
    [chatGuided, composeTimeoutReady],
  );
  // Unsent drafts are keyed by session id (elspeth-ca38667856): ChatPanel
  // stays mounted across session switches, so a bare useState draft typed on
  // session A would still be sitting in session B's composer. Each session
  // owns a slot — switching away hides the draft, switching back restores it.
  // Clearing on switch instead would destroy typed content, which the
  // retention doctrine below forbids. The "" key carries the draft typed
  // while no session is active.
  const draftSessionKey = activeSessionId ?? "";
  // Plain-guided prompt draft (elspeth-49b467d91a). The docked guided composer
  // is CONTROLLED on this state — the same ChatInput controlled mode the
  // tutorial (frozen per-step prompt) and freeform (`inputText`) already use;
  // the plain guided surface was the one uncontrolled instance, so its typed
  // prompt lived only inside ChatInput and was unrecoverable after the
  // clear-on-send. ChatInput still clears this on Send (onChange("")); the
  // retention wrapper below restores it when the send did not deliver.
  const [guidedDraftsBySession, setGuidedDraftsBySession] = useState<
    ReadonlyMap<string, string>
  >(new Map());
  const guidedDraft = guidedDraftsBySession.get(draftSessionKey) ?? "";
  const updateGuidedDraftForSession = useCallback(
    (sessionKey: string, action: SetStateAction<string>) =>
      setGuidedDraftsBySession((drafts) =>
        withSessionDraftSlot(drafts, sessionKey, action),
      ),
    [],
  );
  const setGuidedDraft = useCallback(
    (action: SetStateAction<string>) =>
      updateGuidedDraftForSession(draftSessionKey, action),
    [updateGuidedDraftForSession, draftSessionKey],
  );
  // Parity with the tutorial frame (elspeth-49b467d91a): the tutorial retains
  // its locked prompt until the server-authoritative chat_history carries the
  // user turn (tutorialPromptSentForStep). The live composer gets the same
  // doctrine — retain unless verifiably delivered — by checking, after the
  // send settles, whether the prompt actually landed:
  //   - chat path (a durable checkpoint existed): success appends the user
  //     turn verbatim to chat_history — /guided/chat directly, and the step-3
  //     propose_pipeline prose revision via /guided/respond (R2-F6 transcript
  //     custody). An HTTP failure (5xx/4xx/network), 409 step-conflict resync,
  //     Stop, or client timeout leaves chat_history without it
  //     (sessionStore.chatGuided catch), so the draft is restored for retry.
  //   - start path (compositionState was null → /guided/start): the intent is
  //     durably rooted as a session MESSAGE, not a chat turn, so delivery is
  //     the durable checkpoint chatGuided demands (compositionState non-null).
  // Restore is skipped when the session changed mid-flight: the delivered
  // check reads the ACTIVE session's guided state, so it cannot be evaluated
  // for a session that is no longer active, and restoring unverified would
  // re-offer a prompt that may have been delivered. Restore never clobbers
  // newer typing — the textarea stays editable while the send is pending.
  const sendGuidedChatRetainingDraft = useCallback(
    async (content: string, revisionMode?: GuidedRevisionMode) => {
      const before = useSessionStore.getState();
      const sessionAtSend = before.activeSessionId;
      const hadDurableCheckpoint = before.compositionState !== null;
      const seqFloor = before.guidedSession?.chat_turn_seq ?? 0;
      try {
        await sendGuidedChat(content, revisionMode);
      } finally {
        // finally, not a bare await: chatGuided's offensive-programming
        // guards THROW (precondition violations), and a thrown send
        // delivered nothing — the draft must still be restored before the
        // rejection propagates (review finding on elspeth-49b467d91a).
        const after = useSessionStore.getState();
        if (after.activeSessionId === sessionAtSend) {
          const delivered = hadDurableCheckpoint
            ? (after.guidedSession?.chat_history.some(
                (turn) =>
                  turn.role === "user" &&
                  turn.seq >= seqFloor &&
                  turn.content === content,
              ) ?? false)
            : after.compositionState !== null;
          if (!delivered) {
            // Slot-targeted (sessionAtSend, not the live draftSessionKey):
            // under the same-session guard above they are equal, but the
            // stable identity keeps this callback from churning per switch.
            updateGuidedDraftForSession(sessionAtSend ?? "", (current) =>
              current === "" ? content : current,
            );
          }
        }
      }
    },
    [sendGuidedChat, updateGuidedDraftForSession],
  );
  const cancelGuidedChat = useCallback(() => {
    guidedChatControllerRef.current?.abort(COMPOSE_USER_CANCEL_ABORT_REASON);
  }, []);
  // C-2iii: Retry affordance on a synthetic-failure turn (GuidedChatHistory).
  // Chat turns share one monotonic seq counter (slice 5 invariant), and every
  // assistant reply today is paired with the user turn that triggered it at
  // seq-1 (ChatRole's docstring: Phase A has no unpaired "opener" turns yet).
  // Resend THAT message down the normal chat path — same retry semantics as
  // freeform's MessageBubble Retry, reusing sendGuidedChat rather than a
  // bespoke request — under the OCCURRENCE it was originally submitted for:
  // the persisted turn_token rides the retry verbatim, so a retry whose
  // occurrence has since been answered draws the server's ordinary stale-turn
  // 409 (and its resync) instead of applying old prose to newer session
  // state (elspeth-ea80e34fdc). If no token-bearing user turn exists (a
  // future proactive opener, a transcript-only pair, or any other shape this
  // invariant doesn't cover), there is nothing sound to resend blind;
  // refetch the guided state instead so the wizard resyncs to the server's
  // current truth — the same recovery the turn_not_emitted self-heal uses.
  const handleRetrySyntheticFailure = useCallback(
    (turn: GuidedChatTurn) => {
      if (guidedSession === null || activeSessionId === null) return;
      const preceding = guidedSession.chat_history.find(
        (t) => t.seq === turn.seq - 1,
      );
      if (
        preceding !== undefined &&
        preceding.role === "user" &&
        preceding.turn_token !== null
      ) {
        void sendGuidedChat(preceding.content, undefined, preceding.turn_token);
      } else {
        void startGuided(activeSessionId);
      }
    },
    [guidedSession, activeSessionId, sendGuidedChat, startGuided],
  );

  const {
    sendMessage,
    retryMessage,
    cancelComposition,
    isComposing,
    error,
    errorDetails,
  } = useComposer();

  const messagesEndRef = useRef<HTMLDivElement>(null);
  const scrollContainerRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const guidedLogRef = useRef<HTMLDivElement>(null);
  // Guided-chat pending focus contract (elspeth-6a9673ecd3, placement pass
  // 2026-07-23). The input stays MOUNTED (disabled) while /guided/chat is in
  // flight — the old unmount swap is retired — so into pending there is no
  // programmatic move (yanking focus to the strip down in the conversation
  // flow would be the disruption WCAG 2.4.3 guards against). An Enter-key
  // send keeps focus in the textarea throughout; a mouse-click send drops
  // focus to <body> mid-flight (Send clears the box synchronously → canSend
  // flips false → the focused button disables under the pointer), which the
  // out-of-pending restore below recovers. composerFocusWithinRef tracks
  // whether focus is inside the composer section via bubbled focus/blur on
  // the section — event-driven, so it is already correct BEFORE the pending
  // flag flips.
  const composerSectionRef = useRef<HTMLElement | null>(null);
  const composerFocusWithinRef = useRef(false);
  const prevGuidedChatPendingRef = useRef(guidedChatPending);
  // useLayoutEffect (not useEffect) so no frame paints with focus at body.
  // Out of pending: restore into the composer — to the textarea, or to the
  // section when the tutorial resolve has replaced ChatInput with the
  // static "Sent" line on the same commit the flag drops (inputRef null —
  // React clears detached refs before layout effects). Safe-to-restore
  // mirrors the freeform isComposing restore above: focus stayed inside
  // the composer (Enter path — usually a no-op), OR nobody holds focus at
  // all (the mouse-click path's blur-to-body, which also flipped
  // composerFocusWithinRef false via the section's null-relatedTarget blur).
  // A user who moved away to re-read the transcript holds focus on a real
  // element outside the composer and must not be yanked back. The
  // step-advance effect then owns the move to the fresh decision card.
  useLayoutEffect(() => {
    const was = prevGuidedChatPendingRef.current;
    prevGuidedChatPendingRef.current = guidedChatPending;
    if (guidedChatPending === was) return;
    if (guidedChatPending) return;
    const active = document.activeElement;
    const safeToRestore =
      composerFocusWithinRef.current ||
      active === null ||
      active === document.body;
    if (!safeToRestore) return;
    if (inputRef.current !== null) {
      inputRef.current.focus({ preventScroll: true });
    } else {
      composerSectionRef.current?.focus({ preventScroll: true });
    }
  }, [guidedChatPending]);
  // Guided authoring conversation column (.guided-authoring-scroll). Its own
  // ref + at-bottom tracking — NOT freeform's messagesEndRef/scrollContainerRef
  // machinery, which is keyed to sessionStore.messages and only mounted in the
  // freeform body. Null on every non-guided branch, so the auto-scroll
  // effect below no-ops there. The at-bottom flag is a ref (not state): it is
  // only ever read inside the effect, and a scroll listener that set state
  // would re-render the whole panel on every wheel tick.
  const guidedWorkspaceScrollRef = useRef<HTMLDivElement>(null);
  const guidedWorkspaceAtBottomRef = useRef(true);
  const [showScrollButton, setShowScrollButton] = useState(false);
  const [showBlobManager, setShowBlobManager] = useState(false);
  // Freeform draft: same per-session scoping as guidedDraft above
  // (elspeth-ca38667856).
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
  const [guidedSourceBlobCandidateSet, setGuidedSourceBlobCandidateSet] =
    useState<GuidedSourceBlobCandidateSet | null>(null);
  const pendingGuidedUploadsRef = useRef(new Map<string, GuidedUploadFence>());
  const [pendingGuidedUploads, setPendingGuidedUploads] = useState<
    ReadonlyMap<string, GuidedUploadFence>
  >(new Map());
  const sourceSchemaFormPlugin =
    guidedSession?.step === "step_1_source" && guidedNextTurn?.type === "schema_form"
      ? guidedNextTurn.payload.plugin
      : null;
  const guidedUploadContextRef = useRef<GuidedUploadContext>({
    activeSessionId,
    activationEpoch: blobActivationEpoch,
    step: guidedSession?.step ?? null,
    turnToken: guidedNextTurn?.turn_token ?? null,
    isSourceSingleSelect:
      guidedSession?.step === "step_1_source" &&
      guidedNextTurn?.type === "single_select",
    sourceSchemaFormPlugin,
  });
  guidedUploadContextRef.current = {
    activeSessionId,
    activationEpoch: blobActivationEpoch,
    step: guidedSession?.step ?? null,
    turnToken: guidedNextTurn?.turn_token ?? null,
    isSourceSingleSelect:
      guidedSession?.step === "step_1_source" &&
      guidedNextTurn?.type === "single_select",
    sourceSchemaFormPlugin,
  };
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

  function scrollToBottom() {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
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

  // Guided conversation column: record whether the user sits at the bottom
  // (freeform's 40px heuristic). Measured on scroll — BEFORE any append — so
  // the auto-scroll effect below reads the pre-append position; measuring
  // inside the effect would see the just-appended turn's height and misread
  // "at bottom" as "scrolled up".
  function handleGuidedWorkspaceScroll() {
    const container = guidedWorkspaceScrollRef.current;
    if (!container) return;
    const threshold = 40; // pixels from bottom
    guidedWorkspaceAtBottomRef.current =
      container.scrollHeight - container.scrollTop - container.clientHeight <
      threshold;
  }

  const shouldShowComposerProgress =
    isComposing || isTerminalComposerPhase(composerProgress?.phase);

  // Auto-scroll to bottom when new messages arrive (unless user scrolled up).
  // Empty sessions render template cards above the sentinel; scrolling to the
  // bottom on first paint clips the top row of cards under the header.
  useEffect(() => {
    if (messages.length === 0 && !isComposing) return;
    if (!showScrollButton) {
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    }
  }, [messages, isComposing, showScrollButton]);

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

  const readyGuidedSourceBlobs = useMemo(
    () =>
      new Map(
        blobs
          .filter(
            (blob) =>
              blob.session_id === activeSessionId &&
              blob.status === "ready" &&
              CANONICAL_BLOB_UUID.test(blob.id),
          )
          .map(
            (blob) =>
              [
                blob.id,
                {
                  id: blob.id,
                  filename: blob.filename,
                  sizeBytes: blob.size_bytes,
                } satisfies GuidedSourceBlobCandidate,
              ] as const,
          ),
      ),
    [activeSessionId, blobs],
  );

  const handleGuidedBlobUploaded = useCallback(
    (blob: BlobMetadata) => {
      const turn = guidedNextTurn;
      if (
        activeSessionId === null ||
        blob.session_id !== activeSessionId ||
        blob.status !== "ready" ||
        !CANONICAL_BLOB_UUID.test(blob.id) ||
        guidedSession?.step !== "step_1_source" ||
        turn?.type !== "single_select"
      ) {
        return;
      }
      const candidate: GuidedSourceBlobCandidate = {
        id: blob.id,
        filename: blob.filename,
        sizeBytes: blob.size_bytes,
      };
      setGuidedSourceBlobCandidateSet((current) => {
        if (
          current?.sessionId === activeSessionId &&
          current.turnToken === turn.turn_token
        ) {
          const candidates = [
            ...current.candidates.filter((item) => item.id !== candidate.id),
            candidate,
          ];
          return {
            ...current,
            candidates,
            requiresExplicitChoice:
              current.requiresExplicitChoice || candidates.length > 1,
          };
        }
        return {
          sessionId: activeSessionId,
          turnToken: turn.turn_token,
          candidates: [candidate],
          requiresExplicitChoice: false,
        };
      });
    },
    [activeSessionId, guidedNextTurn, guidedSession?.step],
  );

  const handleGuidedBlobUploadStarted = useCallback(
    (requestId: string, sessionId: string) => {
      const context = guidedUploadContextRef.current;
      if (context.activeSessionId !== sessionId) return;
      pendingGuidedUploadsRef.current.set(requestId, {
        sessionId,
        activationEpoch: context.activationEpoch,
        step: context.step,
        turnToken: context.turnToken,
        isSourceSingleSelect: context.isSourceSingleSelect,
        sourceSchemaFormPlugin: context.sourceSchemaFormPlugin,
      });
      setPendingGuidedUploads(new Map(pendingGuidedUploadsRef.current));
    },
    [],
  );

  const isGuidedBlobUploadFenceCurrent = useCallback(
    (requestId: string, sessionId: string): boolean => {
      const fence = pendingGuidedUploadsRef.current.get(requestId);
      const context = guidedUploadContextRef.current;
      return (
        fence !== undefined &&
        fence.sessionId === sessionId &&
        context.activeSessionId === fence.sessionId &&
        context.activationEpoch === fence.activationEpoch &&
        context.step === fence.step &&
        context.turnToken === fence.turnToken &&
        context.isSourceSingleSelect === fence.isSourceSingleSelect &&
        context.sourceSchemaFormPlugin === fence.sourceSchemaFormPlugin
      );
    },
    [],
  );

  // A source upload completing while the Step-1 schema_form turn is already
  // live (elspeth-c70909c13a): the persisted turn is immutable replay
  // authority, so the server cannot re-emit it prefilled — the blob lands in
  // the form's LOCAL draft instead (the same blob:<id> sentinel the user would
  // legally type). Keyed to the exact turn token so a later turn never
  // inherits it; first compatible upload wins (no silent re-bind).
  const [guidedSourceFormPathPrefill, setGuidedSourceFormPathPrefill] = useState<{
    sessionId: string;
    turnToken: string;
    blob: GuidedSourceBlobCandidate;
  } | null>(null);

  const handleGuidedBlobUploadCompleted = useCallback(
    (requestId: string, sessionId: string, blob: BlobMetadata): boolean => {
      const fence = pendingGuidedUploadsRef.current.get(requestId);
      if (
        fence === undefined ||
        blob.session_id !== sessionId ||
        !isGuidedBlobUploadFenceCurrent(requestId, sessionId)
      ) {
        return false;
      }
      if (fence.isSourceSingleSelect) {
        handleGuidedBlobUploaded(blob);
        return true;
      }
      if (
        fence.sourceSchemaFormPlugin !== null &&
        fence.turnToken !== null &&
        blob.status === "ready" &&
        CANONICAL_BLOB_UUID.test(blob.id) &&
        uploadMatchesSourcePlugin(fence.sourceSchemaFormPlugin, blob.filename)
      ) {
        const turnToken = fence.turnToken;
        setGuidedSourceFormPathPrefill((current) =>
          current?.sessionId === sessionId && current.turnToken === turnToken
            ? current
            : {
                sessionId,
                turnToken,
                blob: {
                  id: blob.id,
                  filename: blob.filename,
                  sizeBytes: blob.size_bytes,
                },
              },
        );
        // Returning false suppresses ChatInput's "please use it as the
        // pipeline input" injection: that message routes to the tool-less
        // Phase-A advisory, which cannot bind the blob and answers with a
        // dead-end "paste the contents" request. The form's path row filling
        // in is the honest feedback for this gesture.
        return false;
      }
      return true;
    },
    [handleGuidedBlobUploaded, isGuidedBlobUploadFenceCurrent],
  );

  const handleGuidedBlobUploadRejected = useCallback(
    (requestId: string, sessionId: string): boolean =>
      isGuidedBlobUploadFenceCurrent(requestId, sessionId),
    [isGuidedBlobUploadFenceCurrent],
  );

  const handleGuidedBlobUploadSettled = useCallback(
    (requestId: string, sessionId: string) => {
      const fence = pendingGuidedUploadsRef.current.get(requestId);
      if (fence === undefined || fence.sessionId !== sessionId) return;
      pendingGuidedUploadsRef.current.delete(requestId);
      setPendingGuidedUploads(new Map(pendingGuidedUploadsRef.current));
    },
    [],
  );

  const hasPendingGuidedSourceUpload = useMemo(
    () =>
      [...pendingGuidedUploads.values()].some(
        (fence) =>
          fence.isSourceSingleSelect &&
          fence.sessionId === activeSessionId &&
          fence.activationEpoch === blobActivationEpoch &&
          fence.step === guidedSession?.step &&
          fence.turnToken === guidedNextTurn?.turn_token &&
          guidedNextTurn?.type === "single_select",
      ),
    [
      activeSessionId,
      blobActivationEpoch,
      guidedNextTurn,
      guidedSession?.step,
      pendingGuidedUploads,
    ],
  );

  // Candidate provenance is scoped to one exact guided turn. Session/turn
  // changes discard the whole set, and the live blob store invalidates files
  // that were deleted or are no longer ready. The derived list repeats those
  // checks synchronously so a stale candidate cannot leak during the effect's
  // cleanup render.
  //
  // guidedSourceBlobCandidateSet is component-local state: a page reload or
  // remount mid step_1_source loses it while the backend's ready blobs
  // survive. When that happens with `current === null`, re-derive the set
  // from the reloaded ready blobs — without this, the chooser silently
  // disappears and the outgoing request omits source_blob_id even though
  // multiple ready blobs exist.
  //
  // A mid-session turn_token rotation while still parked on
  // step_1_source/single_select (e.g. the planner reissuing the step) hits
  // the same symptom: `current` is scoped to the old token and must be
  // invalidated. The effect has no dependency on the state it sets, so if
  // invalidation simply returned null, the re-derive above would only run
  // on the NEXT dependency change (typically the next blobs poll) — the
  // chooser would vanish despite ready blobs being available right now.
  // Falling through to re-derive in this same pass closes that window. The
  // re-derived set mirrors the remount recovery above: it presents ALL
  // ready session blobs, not just the ones accumulated via uploads during
  // the (now-superseded) turn — that is the intended recovery surface.
  useEffect(() => {
    const deriveFromReadyBlobs = (): GuidedSourceBlobCandidateSet | null => {
      if (
        activeSessionId === null ||
        guidedSession?.step !== "step_1_source" ||
        guidedNextTurn?.type !== "single_select" ||
        readyGuidedSourceBlobs.size === 0
      ) {
        return null;
      }
      const candidates = [...readyGuidedSourceBlobs.values()];
      return {
        sessionId: activeSessionId,
        turnToken: guidedNextTurn.turn_token,
        candidates,
        requiresExplicitChoice: candidates.length > 1,
      };
    };

    setGuidedSourceBlobCandidateSet((current) => {
      if (current === null) {
        return deriveFromReadyBlobs() ?? current;
      }
      if (
        activeSessionId === null ||
        guidedSession?.step !== "step_1_source" ||
        guidedNextTurn?.type !== "single_select" ||
        current.sessionId !== activeSessionId ||
        current.turnToken !== guidedNextTurn.turn_token
      ) {
        return deriveFromReadyBlobs();
      }

      const candidates = current.candidates.flatMap((candidate) => {
        const readyCandidate = readyGuidedSourceBlobs.get(candidate.id);
        return readyCandidate === undefined ? [] : [readyCandidate];
      });
      const unchanged =
        candidates.length === current.candidates.length &&
        candidates.every(
          (candidate, index) =>
            candidate.id === current.candidates[index].id &&
            candidate.filename === current.candidates[index].filename &&
            candidate.sizeBytes === current.candidates[index].sizeBytes,
        );
      if (unchanged) return current;
      return {
        ...current,
        candidates,
        // A removed candidate invalidates any selection it may have carried.
        // Keep the turn fail-closed even when filtering leaves one or no files.
        requiresExplicitChoice:
          current.requiresExplicitChoice ||
          candidates.length < current.candidates.length,
      };
    });
  }, [
    activeSessionId,
    guidedNextTurn?.turn_token,
    guidedNextTurn?.type,
    guidedSession?.step,
    readyGuidedSourceBlobs,
  ]);

  const guidedSourceBlobCandidates = useMemo<
    readonly GuidedSourceBlobCandidate[]
  >(() => {
    if (
      guidedSession?.step !== "step_1_source" ||
      guidedNextTurn?.type !== "single_select" ||
      guidedSourceBlobCandidateSet?.sessionId !== activeSessionId ||
      guidedSourceBlobCandidateSet.turnToken !== guidedNextTurn.turn_token
    ) {
      return [];
    }
    return guidedSourceBlobCandidateSet.candidates.flatMap((candidate) => {
      const readyCandidate = readyGuidedSourceBlobs.get(candidate.id);
      return readyCandidate === undefined ? [] : [readyCandidate];
    });
  }, [
    activeSessionId,
    guidedNextTurn,
    guidedSession?.step,
    guidedSourceBlobCandidateSet,
    readyGuidedSourceBlobs,
  ]);

  const guidedSourceBlobChoiceRequired =
    guidedSession?.step === "step_1_source" &&
    guidedNextTurn?.type === "single_select" &&
    guidedSourceBlobCandidateSet?.sessionId === activeSessionId &&
    guidedSourceBlobCandidateSet.turnToken === guidedNextTurn.turn_token &&
    guidedSourceBlobCandidateSet.requiresExplicitChoice;

  // ── Inline-source projection (Phase 5a Task 3) ─────────────────────────────
  //
  // When the active composition's source blob_ref resolves to an assistant-
  // created blob with chat-message provenance, project that blob's metadata +
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
  const blobRef = readBlobRef(compositionState);
  const setInlineSourceSummary = useInlineSourceStore((s) => s.setSummary);
  const clearInlineSourceSummary = useInlineSourceStore((s) => s.clearSummary);
  const inlineSourceSummary = useInlineSourceStore((s) =>
    activeSessionId !== null ? s.summariesBySession[activeSessionId] ?? null : null,
  );

  // ── Interpretation review surfacing ───────────────────────────────────────
  //
  // Both guided and freeform render pending interpretation events through the
  // single AcknowledgementStack (pinned at the top of the chat column), driven
  // by the same `pendingBySession[sessionId]` projection.  The stack owns the
  // ordering, the count announce, the cards, and the foot-of-stack opt-out;
  // ChatPanel only supplies the post-resolve callbacks per mode.

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
  interface ResolveConfirmation {
    id: string;
    userTerm: string;
  }
  const [resolveConfirmations, setResolveConfirmations] = useState<
    ReadonlyArray<ResolveConfirmation>
  >([]);
  // Monotonic counter for confirmation ids — useId is per-component and
  // would collide across appended entries; crypto.randomUUID is overkill
  // for ephemeral UI state. A ref-backed counter is identity-stable across
  // renders and produces predictable, debuggable ids in DevTools.
  const confirmationIdCounterRef = useRef(0);

  const handleInterpretationResolved = useCallback(
    (resolvedEvent: { user_term: string | null }) => {
      // Skip the confirmation when user_term is null — this is the case
      // for opt-out and auto-interpretation rows, which do not have a
      // user term to echo. The opt-out flow has its own confirm dialog
      // ("Stop reviewing interpretations"); a chat-stream
      // echo would be redundant noise.
      const userTerm = resolvedEvent.user_term;
      if (userTerm === null || userTerm === "") return;
      confirmationIdCounterRef.current += 1;
      const id = `resolve-confirmation-${confirmationIdCounterRef.current}`;
      setResolveConfirmations((prev) => [...prev, { id, userTerm }]);
    },
    [],
  );

  // Reset confirmations on session switch — the confirmations are
  // per-session UI state and showing previous-session confirmations in a
  // new session's chat would be confusing.
  useEffect(() => {
    setResolveConfirmations([]);
    confirmationIdCounterRef.current = 0;
  }, [activeSessionId]);

  // Disambiguation re-fire guards (F-10 / F-11). Subscribed via the
  // store so the widget surface updates when a guard flips — without
  // this, clicking "treat as 1 row" once would not remove the widget
  // for the same proposal/message on the next render until something
  // else triggered a re-render.
  const userRequestedSingleRowForMessageIds = useInlineSourceStore(
    (s) => s.userRequestedSingleRowForMessageIds,
  );
  const nonSourceMessageIds = useInlineSourceStore(
    (s) => s.nonSourceMessageIds,
  );
  const addUserRequestedSingleRow = useInlineSourceStore(
    (s) => s.addUserRequestedSingleRow,
  );
  const addNonSourceMessage = useInlineSourceStore(
    (s) => s.addNonSourceMessage,
  );
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
    if (blobRef === null) {
      // No inline source attached — clear any stale projection.
      clearInlineSourceSummary(activeSessionId);
      return;
    }
    let cancelled = false;
    const sessionId = activeSessionId;
    const targetBlobId = blobRef;
    void (async () => {
      try {
        const meta = await getBlobMetadata(sessionId, targetBlobId);
        if (cancelled) return;
        if (!isInlineSourceBlob(meta)) {
          clearInlineSourceSummary(sessionId);
          return;
        }
        const text = await previewBlobContent(sessionId, targetBlobId);
        if (cancelled) return;
        const summary = await projectInlineSourceSummary({
          metadata: meta,
          contentText: text,
          toProvenance: toInlineSourceProvenance,
        });
        if (cancelled) return;
        setInlineSourceSummary(sessionId, summary);
      } catch (err) {
        // Frontend display-projection failure.  Bound `err` (not bare
        // `catch {}`) so programming errors are debuggable:
        //   * Transient blob-fetch failures (Tier-3 boundary —
        //     authenticated endpoint returning 5xx) are the expected
        //     case; we keep the last-known-good summary in the store.
        //   * `toInlineSourceProvenance` throws on an unknown wire
        //     `CreationModality` value (exhaustiveness `never`).  That
        //     would be a wire-contract drift we MUST see, not silently
        //     swallow.
        //   * The Tier-1 hash-invariant throw above lands here.
        // `console.error` follows the in-codebase frontend convention
        // (see App.tsx [preferences], CatalogDrawer.tsx, etc.) — it is
        // NOT the backend `slog` channel (CLAUDE.md "Telemetry and
        // Logging").  The audit trail of the failure itself lives on
        // the server (blob-fetch attempts are recorded server-side);
        // this is the operational mirror so a developer opening
        // devtools sees the projection failure.
        if (!cancelled) {
          console.error("[inline-source] projection failed:", err);
        }
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    activeSessionId,
    blobRef,
    setInlineSourceSummary,
    clearInlineSourceSummary,
  ]);

  // ── Disambiguation candidate set (Phase 5a Task 4) ─────────────────────────
  //
  // A proposal is a disambiguation candidate iff:
  //   1. It is currently pending (status === "pending") and not stale.
  //   2. `isAmbiguousInlineProposal(proposal)` is true (source-only inline
  //      blob + row-count-ambiguity narration phrases).
  //   3. The current known composition has no content that a source-only
  //      set_pipeline could silently replace. If the state is not loaded, the
  //      proposal must itself declare a null base_state_id.
  //   4. We can resolve a non-null originating user message ID for it
  //      (the F-10/F-11 guards need a stable key).
  //   5. The originating message ID is NOT in either re-fire guard
  //      set — the user has already disambiguated in either direction
  //      and we must not re-prompt.
  //
  // Each surviving proposal yields one widget. The matching proposal
  // IDs are also removed from the standard PendingProposalsBanner
  // input set so a single proposal does not appear in BOTH surfaces.
  //
  // useMemo is used here because the computation walks the message
  // list per proposal; recomputing on every render (especially while
  // typing in the chat input, which fires re-renders via the
  // ChatInput's onChange callback) is wasted work.
  const disambiguationCandidates = useMemo(() => {
    return compositionProposals
      .filter((p) => p.status === "pending")
      .filter((p) => !staleProposalIds.includes(p.id))
      .filter(
        (proposal) =>
          isAmbiguousInlineProposal(proposal) &&
          hasSafeInlineSourceDisambiguationBase(proposal, compositionState),
      )
      .map((proposal) => {
        const messageId = findOriginatingMessageId(
          messages,
          proposal.tool_call_id,
        );
        if (messageId === null) return null;
        if (userRequestedSingleRowForMessageIds.has(messageId)) return null;
        if (nonSourceMessageIds.has(messageId)) return null;
        const userMessage = messages.find((m) => m.id === messageId);
        // userMessage existence is implied by findOriginatingMessageId
        // returning a non-null id (it found the id by walking the same
        // messages array), but TypeScript can't see that — narrow with
        // an explicit guard. A null here would be a bug we want to
        // surface, but rather than crash the chat panel we skip the
        // candidate; the standard banner picks it up.
        if (!userMessage) return null;
        return {
          proposal,
          messageId,
          userInput: userMessage.content,
          proposedRows: parseProposedRowsFromUserInput(userMessage.content),
        };
      })
      .filter(
        (c): c is {
          proposal: CompositionProposal;
          messageId: string;
          userInput: string;
          proposedRows: ReadonlyArray<string>;
        } => c !== null,
      );
  }, [
    compositionProposals,
    staleProposalIds,
    compositionState,
    messages,
    userRequestedSingleRowForMessageIds,
    nonSourceMessageIds,
  ]);

  // Proposal IDs claimed by the disambiguation widget — excluded from
  // the standard PendingProposalsBanner so a single proposal does not
  // appear in both surfaces.
  const disambiguationProposalIds = useMemo(
    () => new Set(disambiguationCandidates.map((c) => c.proposal.id)),
    [disambiguationCandidates],
  );

  const bannerProposals = useMemo(
    () =>
      compositionProposals.filter((p) => !disambiguationProposalIds.has(p.id)),
    [compositionProposals, disambiguationProposalIds],
  );

  // ── Disambiguation action handlers ─────────────────────────────────────────
  //
  // Each handler is bound to one button on the widget. The accessible
  // names on those buttons are load-bearing — see the CLOSED LIST
  // comment in InlineSourceDisambiguationTurn.tsx.
  //
  // "Yes — N rows":   delegate to acceptProposal (the standard
  //                   accept-proposal flow lands the inline_blob and
  //                   the inlineSourceStore projection effect picks it
  //                   up on the next composition-state update).
  // "No — treat as 1 row":  reject the proposal + record the F-11 guard
  //                          + ask the LLM to re-interpret as a single
  //                          row. The LLM re-issuing a multi-row
  //                          interpretation for the same message would
  //                          re-fire this widget without the guard.
  // "Edit the rows":   reject the proposal + ask the LLM for an
  //                    in-chat edit of the row list (mirrors the
  //                    chat-mediated edit path Task 3 introduced for
  //                    the post-success surface).
  // "This isn't source data":  record the F-10 guard + tell the LLM
  //                            the message wasn't source data and to
  //                            continue without creating one. We do
  //                            NOT reject the proposal here because
  //                            the F-10 surface implies "stop framing
  //                            my message as source"; the next
  //                            assistant turn will rebuild without
  //                            the inline source and the stale
  //                            proposal will be marked stale by the
  //                            standard rebase pipeline.
  const handleDisambiguationConfirmMultiRow = useCallback(
    (proposalId: string) => {
      void acceptProposal(proposalId);
    },
    [acceptProposal],
  );

  const handleDisambiguationTreatAsOneRow = useCallback(
    (proposalId: string) => {
      const candidate = disambiguationCandidates.find(
        (c) => c.proposal.id === proposalId,
      );
      if (!candidate) return;
      addUserRequestedSingleRow(candidate.messageId);
      void rejectProposal(proposalId);
      sendMessage(
        "Treat my previous message as a single row, not multiple — " +
          "please re-interpret the input as one row and update the source.",
      );
    },
    [
      disambiguationCandidates,
      addUserRequestedSingleRow,
      rejectProposal,
      sendMessage,
    ],
  );

  const handleDisambiguationEditRows = useCallback(
    (proposalId: string) => {
      const candidate = disambiguationCandidates.find(
        (c) => c.proposal.id === proposalId,
      );
      if (!candidate) return;
      void rejectProposal(proposalId);
      const rowsListing = candidate.proposedRows
        .map((row, idx) => `${idx + 1}. ${row}`)
        .join("\n");
      sendMessage(
        `I'd like to edit the proposed row list before continuing.\n\n` +
          `Current rows:\n${rowsListing}\n\n` +
          `Please ask me what changes I want, then update the inline source.`,
      );
    },
    [disambiguationCandidates, rejectProposal, sendMessage],
  );

  const handleDisambiguationNotSourceData = useCallback(
    (messageId: string) => {
      addNonSourceMessage(messageId);
      sendMessage(
        "That message isn't source data — please continue without " +
          "creating a source from it.",
      );
    },
    [addNonSourceMessage, sendMessage],
  );

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
      const prompt =
        `I'd like to edit the inline source "${summary.filename}". ` +
        `Current contents:\n\n${summary.contentPreview}\n\n` +
        `Please update it per the changes I describe in my next message.`;
      sendMessage(prompt);
    },
    [sendMessage],
  );

  // Guided workspace auto-scroll: keep the conversation column pinned to the
  // bottom as turns arrive (chat_history growth) and when either kind of Send
  // starts. Most guided chat uses guidedChatPending; the step-3 proposal
  // composer deliberately delegates to respondGuided and therefore uses
  // guidedResponsePending. Watch both so mounting either pending footer cannot
  // leave the last few pixels below the viewport. Only pin while the reader
  // already sits within 40px of the bottom — a reader scrolled up into the
  // transcript must not be yanked down (freeform's heuristic, tracked
  // pre-append by the onScroll handler above). Deliberately defined BEFORE the
  // step-advance focus effect below: on a Send both fire, and the focus
  // effect's scrollIntoView must win so the just-built decision presents itself.
  const guidedChatHistoryLength = guidedSession?.chat_history.length ?? 0;
  // Identity of the transcript the length above belongs to: the active
  // session while a guided session is loaded, null otherwise. ChatPanel is
  // mounted unkeyed and survives session switches, so without this a switch
  // to a session with a LONGER history — or a freeform→guided flip
  // (length 0→N) — read as chat growth and fired a spurious reveal-scroll
  // that fought the pin-to-bottom on a freshly shown transcript. An identity
  // change re-seeds the previous-length ref instead of comparing across
  // transcripts; only genuine same-session appends reveal.
  const guidedChatIdentity = guidedSession === null ? null : activeSessionId;
  const prevGuidedChatLenRef = useRef(guidedChatHistoryLength);
  const prevGuidedChatIdentityRef = useRef(guidedChatIdentity);
  useEffect(() => {
    const previousLength = prevGuidedChatLenRef.current;
    const identityChanged =
      prevGuidedChatIdentityRef.current !== guidedChatIdentity;
    prevGuidedChatLenRef.current = guidedChatHistoryLength;
    prevGuidedChatIdentityRef.current = guidedChatIdentity;
    const container = guidedWorkspaceScrollRef.current;
    if (!container) return;
    if (!guidedWorkspaceAtBottomRef.current) return;
    container.scrollTop = container.scrollHeight;
    // Guided geometry inverts freeform's: the transcript sits ABOVE the
    // decision card, so on chat_history growth the pin above re-shows the
    // card while the newly appended turn (e.g. an Explain reply, which
    // changes neither step_index nor turn type and so never fires the
    // step-advance effect below) lands above the fold. Reveal it — the
    // stage-divider rows carry no data-seq, so the last [data-seq] row is
    // always the newest conversational turn. On a turn-rebuilding Send the
    // step-advance effect still runs after this one and its scrollIntoView
    // wins (ordering doctrine above).
    if (!identityChanged && guidedChatHistoryLength > previousLength) {
      const rows = container.querySelectorAll(
        ".guided-chat-bubbles [data-seq]",
      );
      rows[rows.length - 1]?.scrollIntoView({
        behavior: "smooth",
        block: "start",
      });
    }
  }, [
    guidedChatHistoryLength,
    // Identity must be a dep: a switch between sessions with EQUAL history
    // lengths otherwise never re-runs the effect, leaving stale refs that
    // suppress (or misread) the next genuine append in the new session.
    guidedChatIdentity,
    guidedChatPending,
    guidedResponsePending,
  ]);

  // Spec §7.4 — maintain focus on the first interactive element of the new turn
  // after step advance.  Without this, a step-advancing button click unmounts
  // the button before the browser can return focus elsewhere, so focus falls to
  // <body>.  Keyboard users then have to Tab from the very top to reach the new
  // turn widget — unacceptable for general a11y.
  //
  // Keyed on step_index AND turn type. Fires when the wizard advances to a new
  // step (step_index changes) OR when a same-step build replaces the turn with a
  // different type — e.g. a `/guided/chat` Send that resolves the source turns
  // single_select → schema_form, or a later server-authored stage transition. The latter
  // matters now the composer is docked at the BOTTOM for every session
  // (including the tutorial): the just-built decision lands ABOVE the box the
  // user just Sent from, so we scroll it into view + focus its first control
  // rather than leaving it off-screen. It deliberately does NOT fire on
  // same-step, same-type store churn (a new TurnPayload object with identical
  // step_index + type) — that would yank focus while the user works the widget
  // (pinned by the "does NOT re-focus … same-step store mutation" test). The
  // ref-null short-circuit handles all non-guided branches implicitly —
  // guidedLogRef.current is null whenever the chat-panel-guided-log div is not
  // mounted (completed surface, freeform surface, no session). Observation
  // elspeth-obs-5ea21f94af documents the original defect and the chosen
  // Option (c) implementation.
  useEffect(() => {
    if (!guidedLogRef.current) return;
    guidedLogRef.current.scrollIntoView({
      behavior: "smooth",
      block: "nearest",
    });
    const first = guidedLogRef.current.querySelector<HTMLElement>(FOCUSABLE_SELECTOR);
    first?.focus({ preventScroll: true });
    // scrollIntoView targets the turn log, which ends before the decision
    // footer. On short viewports that can leave a few pixels of the decision
    // card below the fold. Preserve the reader-scrolled-up guard, but when the
    // conversation was already live at the bottom, finish at the actual end
    // after focus has settled.
    const container = guidedWorkspaceScrollRef.current;
    if (container && guidedWorkspaceAtBottomRef.current) {
      container.scrollTop = container.scrollHeight;
    }
  }, [guidedNextTurn?.step_index, guidedNextTurn?.type]);

  // Present the respond-rejection when it lands. A failed respond (e.g. a
  // wire-confirm 409) mutates ONLY error/errorDetails/guidedResponsePending —
  // nothing the auto-scroll or step-advance effects above watch — and the
  // alert renders as the LAST content of the decision card, which sits at the
  // bottom of a scroll region in both guided layouts. Without this presenter
  // the alert can mount below the fold in the pinned-at-bottom state: a
  // sighted user clicks Confirm and sees nothing but the button re-enable
  // (the elspeth-3b35abf148 variant-3 silent-rejection failure, reintroduced
  // by geometry). block:"nearest" is a no-op when the alert is already in
  // view. Detail-less failures (errorDetails null) keep the always-visible
  // top GuidedErrorBanner and need no scroll — do not key this on `error`.
  const rejectionRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    if (errorDetails == null || errorDetails.length === 0) {
      return;
    }
    rejectionRef.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [errorDetails]);

  const handleSend = useCallback(
    (content: string) => {
      sendMessage(content);
      // Explicit send means user has returned to live conversation —
      // force-scroll to bottom and resume auto-scroll.
      setShowScrollButton(false);
      messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
    },
    [sendMessage],
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

  // ── Guided-mode discriminator ────────────────────────────────────────────────
  //
  // Precedence (intentional):
  //   1. terminal.kind === "completed"  → CompletionSummary surface.
  //   2. active guided session + non-null next turn  → GuidedTurn surface.
  //   3. anything else (no guidedSession, exited_to_freeform terminal, or a
  //      transient state where guidedSession is set but guidedNextTurn is null)
  //      → fall through to the freeform body below.
  //   4. (tutorial only) when `isTutorial` is set, the fall-through in (3) is
  //      replaced by a guided placeholder surface instead of the freeform body,
  //      so a tutorial can never land on a panel-less freeform screen (concern
  //      B). The completed branch (1) still wins for a tutorial completion.
  //
  // The completed branch is checked FIRST so that a stale `guidedNextTurn`
  // alongside a completed terminal still surfaces the summary (correct UX)
  // rather than dispatching a widget.
  //
  // When `terminal.kind === "exited_to_freeform"`, branch 1 does not match
  // (kind !== "completed") and branch 2 does not match (`!guidedSession.terminal`
  // is false because `terminal` is set). Execution falls through to the existing
  // freeform body — which is the correct outcome (non-tutorial only — see point 4)
  // (the user has exited; show them the chat surface).
  //
  // Both branches preserve `id="chat-main"` so the skip-link target is honoured;
  // the modifier class (`--guided` / `--completed`) provides a per-branch hook
  // for future CSS without coupling layout to the freeform surface.
  if (guidedSession?.terminal?.kind === "completed") {
    return (
      <div
        id="chat-main"
        className="chat-panel chat-panel--completed"
        role="region"
        aria-label="Pipeline summary"
      >
        <GuidedWorkflowStepper
          activeStep="ready"
          readyStepLabel={COMPLETED_READY_STEP_LABELS[guidedCompletionOutcome]}
        />
        {error && (
          <GuidedErrorBanner error={error} onDismiss={clearError} />
        )}
        {/* The guided wire-confirm commit surfaces interpretation events
            AFTER the terminal lands (the writer boundary needs the committed
            nodes), so the completion surface is the FIRST place the Accept
            cards can render. Without the stack here the events are orphaned —
            no surface offers resolution and the run gate (tutorial auto-run,
            freeform-style execute) fails closed forever (session e1332b5a).
            Same persistent-mount contract as the guided surface: the stack
            returns null when empty; the live region is unconditional. */}
        <AcknowledgementLiveRegion sessionId={activeSessionId ?? ""} />
        <AcknowledgementStack
          sessionId={activeSessionId ?? ""}
          isTutorial={isTutorial}
          onResolved={(newState) => {
            if (newState !== null) {
              useSessionStore.setState({ compositionState: newState });
            }
          }}
        />
        <CompletionSummary terminal={guidedSession.terminal} isTutorial={isTutorial} />
      </div>
    );
  }

  // STEP_3 renders the guided surface — crucially the docked composer — so the
  // operator can describe the transforms. The 7.1 planner auto-stages a pipeline
  // proposal at Output→Transforms, so step_3 now begins WITH a propose_pipeline
  // turn: a Send here goes through chatGuided, which forwards it to /guided/respond
  // as a prose proposal revision (regenerate the pipeline following the
  // instruction) instead of /guided/chat — /guided/chat rejects a step_3 send with
  // no deferred intents to manage. Render even without a next turn (e.g. a
  // transient proposal-generation failure returned next_turn=null) so the box
  // survives for retry; otherwise the panel falls through to the "Preparing…" flash.
  //
  // The predicate is shared with App (isGuidedBuildActive), which suppresses
  // the freeform SideRail while this branch renders — the workspace rail
  // replaces it. The inline null/terminal re-checks exist only so TypeScript
  // narrows guidedSession inside the branch; they are implied by the helper.
  if (
    guidedSession &&
    !guidedSession.terminal &&
    isGuidedBuildActive(guidedSession, guidedNextTurn)
  ) {
    // Tutorial: the per-stage locked prompt has already been Sent once the
    // current step carries a user turn in the server-authoritative chat_history.
    // Only true after a SUCCESSFUL chatGuided round-trip (an HTTP failure leaves
    // chat_history untouched — see sessionStore.chatGuided catch), so a failed
    // send still shows the box for retry.
    const tutorialPromptSentForStep =
      isTutorial === true &&
      guidedSession.chat_history.some(
        (t) =>
          t.role === "user" &&
          t.step === guidedSession.step &&
          // The Explain button's canned question is NOT the step's prompt:
          // on confirm-only steps it must not flip the locked box to the
          // "Sent" line (exact-string filter; the constant owns the copy).
          t.content !== GUIDED_EXPLAIN_MESSAGE,
      );
    // Only swap the locked box for the static "Sent" line when there is actually
    // a forward affordance to confirm below — the turn widget OR a pending
    // interpretation review. If a Send-driven step was sent but produced neither
    // (e.g. a transient proposal-generation failure returns next_turn=null),
    // keep the box so the learner can retry; hiding it would strand them with no
    // widget and no exit (the tutorial suppresses ExitToFreeform / opt-out).
    const tutorialStepBuilt =
      tutorialPromptSentForStep &&
      (guidedNextTurn != null || hasPendingGuidedInterpretations);
    // Before the locked tutorial prompt is sent, the initial single-select is
    // a rival driver whose choices do not represent the scripted build. After
    // Send, however, a single-select can be the server-authored continuation
    // of a legal component-review Add action. Keep that recovery path live.
    const suppressTutorialSingleSelect =
      isTutorial === true &&
      guidedNextTurn?.type === "single_select" &&
      !tutorialPromptSentForStep;
    // "Describe what you want" composer, docked at the BOTTOM of the panel, full
    // width — the primary chat affordance, mirroring the freeform body's
    // ChatInput which docks below the message log. The tutorial uses the SAME
    // docked position as the live composer (its locked prompt + read-only box,
    // and the "Sent" line after, ride in this same slot); the passive
    // "press Send → confirm what it built" reading order is preserved by the
    // step-advance/type focus effect, which scrolls the just-built decision
    // (above) into view after a Send.
    // It routes plain English through the store's `chatGuided` action, which
    // picks the endpoint by state: the first ordinary message uses /guided/start
    // to establish the durable root; a step-3 proposal-review turn revises the
    // staged proposal via /guided/respond; other later messages use /guided/chat
    // against the existing checkpoint. The caption is keyed on the live step via
    // GUIDED_CHAT_PLACEHOLDERS.
    const stepComposer = (
      <section
        // The composer docks as a plain input strip under the conversation
        // column (freeform's border-top idiom) — no card chrome; the inner
        // .chat-input carries the seam. The section + role=region +
        // aria-label survive in BOTH modes — the "Describe what you want"
        // landmark is load-bearing for AT navigation and the staging e2e
        // locators (tutorial-probe/tutorial-reliability .staging.spec.ts).
        className="guided-step-chat"
        role="region"
        aria-label="Describe what you want"
        // Pending focus contract plumbing (see the useLayoutEffect by the
        // ref declarations). tabIndex=-1 makes the section itself a legal
        // programmatic focus landing (tutorial resolve path). The focus/blur
        // pair track focus-within via bubbling; a null relatedTarget (window
        // blur, click on non-focusable page chrome) counts as "left" — erring
        // that way skips a restore after an app-switch, erring the other way
        // yanks focus back from a user who clicked into the transcript.
        ref={composerSectionRef}
        tabIndex={-1}
        onFocus={() => {
          composerFocusWithinRef.current = true;
        }}
        onBlur={(e) => {
          if (
            !(e.relatedTarget instanceof Node) ||
            !e.currentTarget.contains(e.relatedTarget)
          ) {
            composerFocusWithinRef.current = false;
          }
        }}
      >
        {/* No visible heading — the bare docked strip carries only the
            aria-label for its accessible name (the old dashed-card heading
            was live-guided furniture that died with the pre-workspace
            layout). */}
        {tutorialStepBuilt ? (
          // Tutorial: the locked prompt was already Sent for this step (it is
          // in the transcript above) AND a forward affordance exists below.
          // Replace the redundant active box — which otherwise kept the
          // just-sent prompt with a live Send and read as "did it send?" —
          // with a static confirmation line.
          <p className="guided-step-chat-sent" role="status">
            {/* Honest framing (elspeth-3b45c51564): the tutorial's continue is
                a review-then-proceed, not a choice — don't dress it as one. */}
            Sent — your request is in the transcript above and the assistant
            has built this step. Review the decision, then continue.
          </p>
        ) : (
          <>
            {isProposalRevisionComposer && !isTutorial ? (
              <label className="guided-proposal-revision-scope">
                Revision scope
                <select
                  value={guidedRevisionMode}
                  disabled={guidedResponsePending || guidedChatPending}
                  onChange={(event) => setGuidedRevisionSelection({
                    identity: proposalRevisionIdentity,
                    mode: event.target.value as GuidedRevisionMode,
                  })}
                >
                  <option value="amend">Amend current proposal — preserve existing steps</option>
                  <option value="replace">Replace proposal — steps may be removed</option>
                </select>
              </label>
            ) : null}
            <ChatInput
              onSend={(content) => {
                // Tutorial sends keep the plain action: retention is the
                // frozen locked-prompt value itself, and writing a tutorial
                // prompt into guidedDraft would leak it into a later live
                // guided surface on the same session.
                if (isTutorial === true) {
                  void sendGuidedChat(content);
                  return;
                }
                if (isProposalRevisionComposer) {
                  const liveState = useSessionStore.getState();
                  const liveTurn = liveState.guidedNextTurn;
                  const liveProposalRevisionIdentity =
                    liveState.guidedSession?.step === "step_3_transforms" &&
                    liveTurn?.type === "propose_pipeline"
                      ? `${liveState.activeSessionId ?? ""}:${liveTurn.payload.proposal_id}:${liveTurn.payload.draft_hash}`
                      : null;
                  const selectedMode =
                    guidedRevisionSelection.identity === liveProposalRevisionIdentity
                      ? guidedRevisionSelection.mode
                      : "amend";
                  // No eager reset-to-amend here: a DELIVERED revision
                  // advances the proposal identity, and the identity-keyed
                  // effect above already resets the selector; an eager reset
                  // made a FAILED send restore the prompt but silently flip
                  // the user's "replace" choice back to "amend", so the
                  // retry resubmitted a semantically different request
                  // (review finding on elspeth-49b467d91a). Draft and scope
                  // now retain or clear together.
                  void sendGuidedChatRetainingDraft(content, selectedMode);
                  return;
                }
                void sendGuidedChatRetainingDraft(content);
              }}
            onBlobUploadStarted={handleGuidedBlobUploadStarted}
            onBlobUploadCompleted={handleGuidedBlobUploadCompleted}
            onBlobUploadRejected={handleGuidedBlobUploadRejected}
            onBlobUploadSettled={handleGuidedBlobUploadSettled}
            uploadDisabled={hasPendingGuidedSourceUpload}
            // Both pendings gate Send. `guidedResponsePending` blocks a chat
            // WHILE a turn-respond is advancing the step — otherwise the chat
            // captures the stale `guidedSession.step` and the backend rejects
            // the step mismatch with 409 (guided.py step-match guard).
            // `guidedChatPending` blocks a second send racing the in-flight
            // one (belt: ChatInput's handleSend returns early on disabled;
            // braces: sendGuidedChat's own pending admission gate). The old
            // pending SWAP — unmounting the input and mounting the working
            // strip in its place (elspeth-6a9673ecd3) — is retired: it read
            // as a large panel occluding the typing area (operator
            // 2026-07-23). The strip now rides in the conversation flow (see
            // the guided-authoring-scroll mount); the input keeps its place
            // with its ordinary disabled state, which also preserves any
            // typed draft. Stop stays in the strip (it renders only while
            // the abortable chat fetch exists).
            disabled={guidedResponsePending || guidedChatPending}
            inputRef={inputRef}
            placeholder={GUIDED_CHAT_PLACEHOLDERS[guidedSession.step]}
            maxLength={GUIDED_CHAT_MESSAGE_MAX_LENGTH}
            // Tutorial: the box is locked read-only and prefilled with the
            // CURRENT phase's per-stage prompt (wire has none → empty,
            // confirm-only). Kept controlled (value defined) across all phases
            // to avoid controlled↔uncontrolled flips. Normal session:
            // controlled on guidedDraft (elspeth-49b467d91a) so the typed
            // prompt is parent-owned and sendGuidedChatRetainingDraft can
            // restore it when a send fails to deliver — never uncontrolled.
            value={
              isTutorial
                ? (lockedChatPrompt?.[guidedSession.step] ?? "")
                : guidedDraft
            }
            onChange={isTutorial ? () => undefined : setGuidedDraft}
            readOnly={isTutorial === true}
            />
          </>
        )}
      </section>
    );

    // The "current decision" panel (eyebrow + per-step rationale + the turn
    // widget). Rendered LAST inside the conversation-column scroll region
    // (.guided-authoring-scroll) — the action zone between the reply and the
    // composer. It is deliberately NOT docked with the composer:
    // a tall schema/wire widget in a fixed dock crushes the transcript (the
    // recorded tutorial.css fill-viewport failure); inside the scroll region
    // the step-advance focus effect scrolls it into view after a Send instead.
    const decisionSection = (() => {
      const stepIsSendDriven =
        isTutorial && (lockedChatPrompt?.[guidedSession.step] ?? "") !== "";
      // Lead the decision with the dynamic build rationale (the LLM's "what I
      // built" summary for this step); fall back to the static step purpose when
      // no assistant turn exists yet so the headline is never blank.
      const rationale = latestAssistantRationale(guidedSession);
      return (
        <section
          className={
            stepIsSendDriven
              ? "guided-current-decision guided-current-decision--tutorial"
              : "guided-current-decision"
          }
          aria-labelledby="guided-current-decision-heading"
        >
          <div className="guided-current-decision-copy">
            <p className="guided-current-decision-eyebrow" aria-hidden="true">
              Current decision
            </p>
            <h2
              id="guided-current-decision-heading"
              className="guided-current-decision-rationale"
            >
              {rationale ?? GUIDED_STEP_PURPOSES[guidedSession.step]}
            </h2>
            {stepIsSendDriven && !tutorialStepBuilt && (
              <p className="guided-current-decision-tutorial-note">
                {/* Directionally neutral (elspeth-eba8820005): the Send button
                    sits RIGHT of the textarea and the columns reflow across
                    breakpoints, so "below" is wrong somewhere for everyone. */}
                You don't need to fill this in by hand — press the{" "}
                <strong>Send</strong> button and the assistant builds this step.
                Then review the decision and continue.
              </p>
            )}
          </div>
          <div
            ref={guidedLogRef}
            className="chat-panel-guided-log"
            role="log"
            aria-label="Guided wizard step"
            aria-live="polite"
            aria-relevant="additions"
          >
            {/* Interpretation reviews moved above the intent box (approve-before-
                advance); the turn widget remains here as the current decision. */}
            {guidedNextTurn && !suppressTutorialSingleSelect && (
              <GuidedTurn
                turn={guidedNextTurn}
                onSubmit={(body) => void respondGuided(body)}
                sourceBlobCandidates={guidedSourceBlobCandidates}
                sourceBlobChoiceRequired={guidedSourceBlobChoiceRequired}
                sourceUploadPending={hasPendingGuidedSourceUpload}
                sourceFormPathPrefill={
                  guidedSourceFormPathPrefill !== null &&
                  guidedSourceFormPathPrefill.sessionId === activeSessionId &&
                  guidedSourceFormPathPrefill.turnToken === guidedNextTurn.turn_token
                    ? guidedSourceFormPathPrefill.blob
                    : null
                }
                // M-10: gate on guidedChatPending too — otherwise a chip/form
                // widget stays clickable while a /guided/chat is in flight and
                // can race an in-flight step-respond (mirrors "Explain this
                // step"'s gating just below).
                disabled={
                  guidedResponsePending ||
                  guidedChatPending ||
                  hasPendingGuidedInterpretations
                }
                isTutorial={isTutorial}
                proposalReviewState={guidedProposalReview}
                wirePendingAcknowledgements={
                  hasPendingGuidedInterpretations
                    ? wirePendingAcknowledgements
                    : undefined
                }
                wireValidationIssues={wireValidationIssues}
              />
            )}
          </div>
          {/* Card footer: anchors the card's ambient furniture on one seam —
              the pending "Saving decision" status (left) and the one-click
              "why am I seeing this?" Explain affordance (right) — instead of
              a floating bottom-right button over dead space plus a bare
              status paragraph (operator-reported). Explain sends a canned
              question down the NORMAL guided-chat path (user turn + assistant
              bubble in the transcript; the pending strip shows while it
              runs). The backend advisory prompt carries the LLM-safe
              current-build context, so the answer names the actual
              plugins/settings on screen. Only offered when a decision is
              actually showing; disabled while any chat/respond is in flight
              (same 409 guard as the composer). */}
          {(guidedNextTurn !== null || guidedResponsePending) && (
            <div className="guided-current-decision-footer">
              {guidedResponsePending && (
                <p
                  className="guided-current-decision-pending guided-decision-pending"
                  role="status"
                >
                  {/* Chat Send pending idiom (pulse + adaptive headline +
                      elapsed readout): a decision submit can front a
                      multi-minute planner run and must not read as stalled.
                      The headline resolves to the live composer-progress
                      phase when one is being published, else this copy. */}
                  <GuidedDecisionPendingIndicator
                    fallback="Saving decision..."
                    composerProgress={composerProgress}
                  />
                </p>
              )}
              {guidedNextTurn && (
                <Button
                  compact
                  // Composes .btn-compact (the shared.css:222 idiom). The
                  // pre-migration class list carried a redundant `.btn` token;
                  // every declaration it contributed is overridden by the
                  // later .btn-compact block at equal specificity (or
                  // duplicated verbatim in its hover/disabled rules), so
                  // dropping it is computed-style inert.
                  className="guided-explain-btn"
                  onClick={() => void sendGuidedChat(GUIDED_EXPLAIN_MESSAGE)}
                  // Bootstrap race: Explain routes sendGuidedChat ->
                  // runComposeWithTimeout, which no-ops until the backend wall
                  // clock lands. A guided decision restores from server state on
                  // reload before App.checkHealth latches readiness, so gate the
                  // button (dead-button doctrine) rather than leave a silent
                  // no-op — same as the primary Send.
                  disabled={
                    guidedChatPending ||
                    guidedResponsePending ||
                    !composeTimeoutReady
                  }
                  title={
                    !guidedChatPending &&
                    !guidedResponsePending &&
                    !composeTimeoutReady
                      ? composerTimeoutUnavailable
                        ? COMPOSE_UNAVAILABLE_MESSAGE
                        : COMPOSE_CONNECTING_MESSAGE
                      : undefined
                  }
                >
                  Explain this step
                </Button>
              )}
            </div>
          )}
          {/* C-3 self-heal notice (turn_not_emitted): a calm resync message,
              deliberately role="status" (polite) rather than role="alert"
              like the rejection banner below — the wizard resyncing itself
              is not a failure, so it must not read as one. Cleared on the
              next guided respond/chat attempt (sessionStore) or by
              dismissing the generic error. */}
          {guidedSelfHealNotice && (
            <p className="guided-current-decision-pending" role="status">
              {guidedSelfHealNotice}
            </p>
          )}
          {/* Backend rejection surfaced NEXT TO the turn widget it rejected —
              never only the status strip, never silent (elspeth-3b35abf148
              variant 3). `errorDetails` is only populated by guided respond
              rejections; the generic top banner is suppressed while this
              renders so the alert announces once. */}
          {error && errorDetails != null && errorDetails.length > 0 && (
            <div ref={rejectionRef} role="alert" className="guided-respond-rejection">
              <p className="guided-respond-rejection-message">{error}</p>
              <ul className="guided-respond-rejection-details">
                {errorDetails.map((detail, index) => (
                  <li key={index}>{detail}</li>
                ))}
              </ul>
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
        </section>
      );
    })();

    return (
      <div
        id="chat-main"
        className="chat-panel chat-panel--guided"
        role="region"
        aria-label="Guided composer"
      >
        {/* Header — mirrors the freeform body header so the mode-switch control
            ("Exit to freeform") sits in the same top-right spot that freeform's
            "Switch to guided" occupies. The tutorial suppresses the exit
            affordance, so it has no header. Session title and model identity
            are app-level facts and live in AppHeader (elspeth-8fa71e6d15) —
            this column keeps only compose-state chrome. */}
        {!isTutorial && (
          <div className="chat-panel-header">
            <div className="chat-panel-header-actions">
              <ModeSwitchButton target="freeform" hasWork={currentChatHasWork} />
            </div>
          </div>
        )}
        <GuidedWorkflowStepper activeStep={guidedSession.step} />
        {/* Suppressed while the inline respond-rejection alert renders next to
            the turn widget (errorDetails non-empty) — one alert, one announce. */}
        {error && (errorDetails == null || errorDetails.length === 0) && (
          <GuidedErrorBanner error={error} onDismiss={clearError} />
        )}
        {/* "What just happened / what to do" surfaces. The common
            ComposerWorkspace owns artifact and inspector content; this branch
            supplies only the guided authoring pane. */}
        {(() => {
          // Shared pieces. aria-live rationale: each piece owns its OWN live
          // region — GuidedChatHistory (role=log), AcknowledgementLiveRegion
          // (role=status) — announcing independently of wizard advances.
          // Resolved GuidedHistory lives in the common Inspector History tab.
          // The live TURN surface lives in decisionSection's own role=log
          // wrapper — load-bearing for InspectAndConfirmTurn, which omits its
          // own warnings live region under the convention that the parent
          // wraps turn content.
          const transcript = (
            <GuidedChatHistory
              chatHistory={guidedSession.chat_history}
              onRetrySyntheticFailure={handleRetrySyntheticFailure}
              // !composeTimeoutReady: the synthetic-failure turn restores from
              // server-authoritative chat_history on reload, so its Retry can
              // render before readiness latches. Retry routes sendGuidedChat ->
              // runComposeWithTimeout, which no-ops when not ready; disable it
              // so it is not a silent dead button (same gate as Explain).
              retryDisabled={
                guidedChatPending || guidedResponsePending || !composeTimeoutReady
              }
            />
          );
          // Persistent-mount contract (AcknowledgementStack.tsx): the stack
          // returns null when empty, so the count announcer must live outside
          // it, unconditionally rendered — and in the tutorial OUTSIDE the
          // scroll wrapper, whose subtree is where content churns.
          const ackLiveRegion = (
            <AcknowledgementLiveRegion sessionId={activeSessionId ?? ""} />
          );
          const ackStack = (
            <AcknowledgementStack
              sessionId={activeSessionId ?? ""}
              isTutorial={isTutorial}
              onResolved={(newState) => {
                if (newState !== null) {
                  useSessionStore.setState({ compositionState: newState });
                }
              }}
            />
          );
          return (
            <>
              {ackLiveRegion}
              {/* role="group" is REQUIRED for the aria-label to be exposed
                  (a name on a role-less div is AT-invisible and an axe
                  aria-prohibited-attr violation, elspeth-37293a3b7c).
                  Deliberately NOT role=log / NOT a live region: the
                  transcript log and the wizard log live INSIDE it and must
                  not nest in an outer live region (double-announce).
                  tabIndex=0 lets keyboard users arrow-scroll it. */}
              <div
                ref={guidedWorkspaceScrollRef}
                onScroll={handleGuidedWorkspaceScroll}
                className="guided-authoring-scroll"
                role="group"
                aria-label="Conversation"
                tabIndex={0}
              >
                {transcript}
                {ackStack}
                {decisionSection}
                {/* Direct child of the scroll region: OUTSIDE both role=log
                    containers, so its role=status is announced once. */}
                {guidedChatPending ? (
                  <GuidedPendingStrip
                    composerProgress={composerProgress}
                    onStop={cancelGuidedChat}
                    substeps={
                      isTutorial === true && guidedSession.step === "step_2_sink"
                        ? TUTORIAL_STEP_2_COMPOSING_SUBSTEPS
                        : undefined
                    }
                    activeSubstepIndex={tutorialStep2ActiveSubstep(
                      composerProgress?.phase,
                    )}
                  />
                ) : null}
              </div>
              {stepComposer}
            </>
          );
        })()}
      </div>
    );
  }

  // ── Concern B: a tutorial shows the guided placeholder while loading ──
  //
  // Reaching this point means neither the completed branch nor the
  // guided-active branch matched. For a non-tutorial session that is the
  // legitimate freeform surface (below). For a TUTORIAL session, the
  // placeholder covers the TutorialGuidedShell startup window: guidedSession
  // is transiently null before the async start resolves, and a non-null
  // pre-turn session is the gap during startGuided.
  //
  // An `exited_to_freeform` terminal is deliberately EXCLUDED and falls
  // through to the freeform body: the wire-stage "Exit to freeform" button
  // IS reachable in tutorial mode (on blocked outcomes it is the only
  // affordance), and the old always-placeholder guard turned that terminal
  // into a permanent "Preparing…" dead-end — nothing ever cleared it
  // (elspeth-61591e64bb). On exit the tutorial shell's onExited hand-off
  // persists the opt-out and unmounts the shell; rendering the freeform
  // body here is the correct surface for the brief in-flight window.
  //
  // The rail falls back to "step_1_source" ONLY for the startup-flash case
  // where `guidedSession === null` (no step exists yet); a non-null session
  // carries its real step. Hardcoding step_1 in the non-null case would
  // show the wrong step in the rail — a fidelity gap.
  if (isTutorial && guidedSession?.terminal?.kind !== "exited_to_freeform") {
    const placeholderStep: WorkflowStepId = guidedSession?.step ?? "step_1_source";
    return (
      <div
        id="chat-main"
        className="chat-panel chat-panel--guided"
        role="region"
        aria-label="Guided composer"
        data-testid="tutorial-guided-loading"
      >
        <GuidedWorkflowStepper activeStep={placeholderStep} />
        <p role="status" className="guided-loading-status">
          Preparing your guided pipeline…
        </p>
      </div>
    );
  }

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
      {/* Compose-state header. The "Switch to guided" affordance lives in the
          header so it's always visible without competing with the chat input
          for vertical real-estate. Symmetric with the "Exit to freeform"
          control in the guided branch above — both are the same
          ModeSwitchButton, so they share the conditional-confirm behaviour.
          Session title and model identity are app-level facts and live in
          AppHeader (elspeth-8fa71e6d15): a second in-column copy competed
          with these controls for a 360px rail and truncated at every width.
          The AuthorityChip stays — it is the one fact this audit-first
          product must keep visible at a glance (chat.css:693). */}
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
          <ModeSwitchButton
            target="guided"
            hasWork={currentChatHasWork}
            disabledReason={guidedSwitchDisabledReason}
          />
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

      {/*
        Acknowledgement stack — pinned at the top of the chat column.  Both
        guided and freeform unify on this surface; in freeform it sits above
        the scrolling message list so pending decisions stay visible.  The
        resolved event is surfaced so the "Got it…" confirmation bubble below
        can read user_term; applyResolvedInterpretation re-syncs + re-validates
        so the run gate opens once the last decision is acknowledged.  Opt-out
        passes a null event so no per-term confirmation fires.
      */}
      {activeSessionId !== null && (
        <>
          {/*
            Persistent count announcer (see the guided branch / the component
            doc): pre-exists its content so the 0→1 appearance announces.
          */}
          <AcknowledgementLiveRegion sessionId={activeSessionId} />
          <AcknowledgementStack
            sessionId={activeSessionId}
            onResolved={(newState, event) => {
              applyResolvedInterpretation(newState);
              if (event !== null) {
                handleInterpretationResolved(event);
              }
            }}
          />
        </>
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
            chatTurns
              .filter(
                (turn: ChatTurn, index: number) =>
                  turn.isComplete || !isComposing || index < chatTurns.length - 1,
              )
              .map((turn: ChatTurn) => {
                const repr = turnRepresentativeMessage(turn);
                // Attach the inline-source summary to the most recent complete
                // agent turn — that's the turn whose audit narrative includes
                // the source-creation event. The store holds at most one
                // summary per session today; passing it as a list keeps the
                // bubble's contract ready for multi-source turns without a
                // future refactor here. When no agent turn is present (e.g.
                // session-restore loaded a composition before any chat), the
                // summary falls through to the standalone widget rendered
                // below the message stream.
                const sourcesForThisTurn =
                  inlineSourceSummary && turn.id === inlineSourceTargetTurnId
                    ? [inlineSourceSummary]
                    : undefined;
                return (
                  <MessageBubble
                    key={turn.id}
                    message={repr}
                    isComposing={isComposing}
                    onRetry={turn.kind === "user" ? retryMessage : undefined}
                    onFork={turn.kind === "user" ? handleFork : undefined}
                    proposalsByToolCallId={proposalsByToolCallId}
                    compositionState={compositionState}
                    staleProposalIds={staleProposalIds}
                    proposalActionPendingIds={proposalActionPendingIds}
                    onAcceptProposal={acceptProposal}
                    onRejectProposal={rejectProposal}
                    sourcesCreated={sourcesForThisTurn}
                    onEditInlineSource={handleEditInlineSource}
                  />
                );
              })
          )}
          {/* Standalone fallback for the inline-source summary: only painted
              when no complete agent turn exists to absorb it into the bubble
              (e.g. session-restore where a composition was loaded before any
              chat turn happened). The hybrid keeps the operator's stated UX —
              sources-created appears inside the bubble like tool calls do —
              while not silently dropping the summary in pre-chat states. */}
          {inlineSourceSummary && inlineSourceTargetTurnId === null && (
            <InlineSourceCreatedTurn
              summary={inlineSourceSummary}
              onEdit={handleEditInlineSource}
            />
          )}
          {/*
            Resolve-success confirmation bubbles (Phase 5b.18b.8).

            One assistant-styled bubble per resolved interpretation. Rendered
            inside the role="log" region so the new bubble is announced to
            AT users on append (aria-live="polite" on the parent). The
            bubbles compose the same message-row--assistant +
            bubble-assistant classes as ordinary assistant turns
            (MessageBubble.tsx) so the confirmation visually flows with the
            conversation — the previous chat-message* tokens here were
            defined by no stylesheet, so these rendered as bare unstyled
            text (elspeth-729872658a). These are NOT persisted to
            sessionStore.messages and do NOT round-trip to the server — see
            the handleInterpretationResolved comment above for the
            rationale.
          */}
          {resolveConfirmations.map((conf) => (
            <div
              key={conf.id}
              className="message-row message-row--assistant interpretation-review-confirmation"
              data-testid="interpretation-review-confirmation"
              role="status"
            >
              <div className="bubble bubble-assistant message-bubble-content">
                Got it — using your interpretation of{" "}
                <em className="interpretation-review-confirmation-user-term">
                  {conf.userTerm}
                </em>
                .
              </div>
            </div>
          ))}
          {/*
            Inline-source disambiguation widgets (Phase 5a Task 4).

            One widget per pending+non-stale ambiguous-inline proposal
            that survives the F-10 / F-11 re-fire guards (see
            `disambiguationCandidates` derivation above for the
            predicate). Each widget claims its proposal id; the same id
            is excluded from `bannerProposals` so the standard
            PendingProposalsBanner does not duplicate the action
            surface. Non-ambiguous proposals continue to route through
            the banner unchanged.
          */}
          {disambiguationCandidates.map((candidate) => (
            <InlineSourceDisambiguationTurn
              key={candidate.proposal.id}
              userInput={candidate.userInput}
              proposedRows={candidate.proposedRows}
              proposalId={candidate.proposal.id}
              messageId={candidate.messageId}
              onConfirmMultiRow={handleDisambiguationConfirmMultiRow}
              onTreatAsOneRow={handleDisambiguationTreatAsOneRow}
              onEditRows={handleDisambiguationEditRows}
              onNotSourceData={handleDisambiguationNotSourceData}
            />
          ))}
          <div ref={messagesEndRef} />
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

      {/* Pending-proposal banner — surfaces composer proposals that need
          operator approval, co-located with the input so the user does not
          have to scroll up to find the Accept button on the originating
          tool-call message. Component returns null when nothing is pending. */}
      {/* Phase 5a Task 4: `bannerProposals` excludes any proposal
          currently surfaced by an InlineSourceDisambiguationTurn
          widget above so a single proposal does not appear in BOTH
          surfaces. The widget handlers ultimately funnel through
          acceptProposal / rejectProposal so the audit chain is
          identical regardless of which surface lands the action. */}
      <PendingProposalsBanner
        proposals={bannerProposals}
        staleProposalIds={staleProposalIds}
        proposalActionPendingIds={proposalActionPendingIds}
        onAccept={acceptProposal}
        onReject={rejectProposal}
      />

      {/*
        Inline-source fallback prompt (Phase 5a Task 5).

        LLM-skip safety net. Anchored ABOVE the chat input — the user
        reads the affordance immediately before the surface they would
        otherwise re-type into. The widget renders nothing when
        `shouldRenderFallback` is false; mounting unconditionally with
        the boolean gate keeps the DOM stable across predicate flips
        (no remount churn for the focus/scroll containers around it).
      */}
      <InlineSourceFallbackPrompt
        shouldRender={shouldRenderFallback}
        candidateText={fallbackCandidate ?? ""}
        onAccept={handleFallbackAccept}
        onDismiss={handleFallbackDismiss}
      />

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

type WorkflowStepId = GuidedStep | "ready";

const GUIDED_STEP_PURPOSES: Record<GuidedStep, string> = {
  step_1_source: "Choose the input and confirm what ELSPETH can read.",
  step_2_sink: "Choose the output shape and the fields the pipeline should produce.",
  step_3_transforms: "Review the transform stages that turn source data into the output.",
  step_4_wire: "Review and confirm the wiring between your pipeline steps.",
};

const GUIDED_WORKFLOW_STEPS: ReadonlyArray<{
  id: WorkflowStepId;
  label: string;
}> = [
  { id: "step_1_source", label: GUIDED_STEP_LABELS.step_1_source },
  { id: "step_2_sink", label: GUIDED_STEP_LABELS.step_2_sink },
  { id: "step_3_transforms", label: GUIDED_STEP_LABELS.step_3_transforms },
  { id: "step_4_wire", label: GUIDED_STEP_LABELS.step_4_wire },
  // "ready" is a stepper-only pseudo-step, not a GuidedStep — its label
  // stays local rather than widening the shared wire-keyed map.
  { id: "ready", label: "Ready" },
];

// Terminal-step label on the COMPLETED surface (elspeth-bf9c296ee5): the
// pseudo-step stays fifth and current (the wizard genuinely finished), but
// "Ready" is only honest once the run gate would pass. While pending
// acknowledgement cards block execution the stage is "Review"; before the
// backend admits the pipeline it is "Validation". Mid-wizard steppers keep
// the default "Ready" — there the step is a yet-to-be-reached goal, not a
// claim about the present. response_ready is unreachable on a guided
// completion (its mutation axis is fixed true); mapped for totality.
const COMPLETED_READY_STEP_LABELS: Record<CompletionOutcome, string> = {
  review_required: "Review",
  pipeline_updated: "Validation",
  pipeline_ready: "Ready",
  response_ready: "Ready",
};

function GuidedWorkflowStepper({
  activeStep,
  readyStepLabel = "Ready",
}: {
  activeStep: WorkflowStepId;
  readyStepLabel?: string;
}) {
  const activeIndex = GUIDED_WORKFLOW_STEPS.findIndex((step) => step.id === activeStep);
  return (
    <nav className="guided-workflow" aria-label="Guided workflow progress">
      {/* The ol carries no aria-label of its own — the nav already announces
          "Guided workflow progress"; two nested near-identical labels are
          screen-reader noise (stepper visual-language spec,
          elspeth-8fa71e6d15). */}
      <ol className="guided-workflow-list">
        {GUIDED_WORKFLOW_STEPS.map((step, index) => {
          const state =
            index < activeIndex
              ? "complete"
              : index === activeIndex
                ? "current"
                : "upcoming";
          return (
            <li
              key={step.id}
              className={`guided-workflow-step guided-workflow-step--${state}`}
              aria-current={state === "current" ? "step" : undefined}
            >
              {/* Indicator is aria-hidden in every state: list order already
                  conveys position, a bare numeral adds noise, and the check
                  is purely visual. Complete/upcoming get a visually-hidden
                  state suffix because with the indicator hidden from AT the
                  distinction is otherwise visual-only; current needs none —
                  aria-current announces it. */}
              <span className="guided-workflow-index" aria-hidden="true">
                {state === "complete" ? (
                  <svg
                    className="guided-workflow-check"
                    viewBox="0 0 24 24"
                    focusable="false"
                    aria-hidden="true"
                  >
                    <path d="M5 13l4 4L19 7" />
                  </svg>
                ) : (
                  index + 1
                )}
              </span>
              <span className="guided-workflow-label">
                {step.id === "ready" ? readyStepLabel : step.label}
              </span>
              {state !== "current" && (
                <span className="visually-hidden">
                  {state === "complete" ? ", completed" : ", not started"}
                </span>
              )}
            </li>
          );
        })}
      </ol>
    </nav>
  );
}

function GuidedErrorBanner({
  error,
  onDismiss,
}: {
  error: string;
  onDismiss: () => void;
}) {
  return (
    <div role="alert" className="chat-panel-error">
      <span>{error}</span>
      <Button
        variant="bare"
        onClick={onDismiss}
        className="chat-panel-error-dismiss"
        aria-label="Dismiss error"
      >
        {"\u00D7"}
      </Button>
    </div>
  );
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
