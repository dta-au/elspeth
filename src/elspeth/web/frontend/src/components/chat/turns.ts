// Render-time coalescing of audit-grade chat messages into user-facing turns.
//
// The backend persists one chat_messages row per LLM round-trip — required by
// Tier-1 audit doctrine. A single user prompt that triggers multiple sequential
// tool-call rounds therefore lands as one user row + N assistant rows in the DB
// (with role="tool" rows interleaved when audit views opt in). Rendering those
// N rows as N separate bubbles leaks audit granularity into the chat UI; the
// user thinks of "what the agent did in response to my message" as a single
// turn.
//
// This module projects the audit stream into user-visible turns without
// mutating the underlying messages. Each turn carries the underlying rows so
// the rendering layer can still attribute, key, and link back to audit
// artifacts when needed.
import type { ChatMessage, ToolCall } from "@/types/api";

export type ChatTurnKind = "user" | "agent" | "system";

export interface ChatTurn {
  /** Stable key — first underlying message's id. */
  id: string;
  kind: ChatTurnKind;
  /** Underlying audit-grade rows in the turn, in original order. */
  messages: ChatMessage[];
  /** Union of tool_calls across every message in the turn, in emission order. */
  aggregatedToolCalls: ToolCall[];
  /**
   * Last non-empty `content` from a GENUINE REPLY row — an assistant row with
   * no tool_calls (see `isGenuineReply`). "" when the turn has produced none:
   * either it is still mid-flight, or it ended without the composer replying
   * (convergence / timeout). Content on tool-call rows is the model's
   * narration on its way to a call — internal scratchpad, never the answer
   * (elspeth-e074575b6e).
   */
  finalContent: string;
  /**
   * The message to attribute the bubble to for actions like copy/retry/fork:
   * the last genuine-reply row with non-empty content if one exists, else the
   * last message in the turn. For user/system turns this is the sole message.
   */
  primaryMessage: ChatMessage;
  /**
   * Atomic-reveal contract: true once the turn is safe to render in the chat.
   *
   * - `user` / `system` turns are standalone audit rows and always complete.
   * - `agent` turns become complete the moment a GENUINE REPLY row carries
   *   non-empty content (i.e. the LLM's text reply has landed). While the turn
   *   contains only tool-call rows — with or without narration on them — it
   *   stays incomplete and the rendering layer hides the bubble (a placeholder
   *   "thinking" affordance occupies the slot instead, painted by ChatPanel
   *   based on isComposing).
   *
   * A turn can therefore stay incomplete permanently when the compose loop
   * ended without replying. ChatPanel resolves that with its own terminal
   * signal (`isComposing === false` means nothing more is coming), rendering
   * the turn so its tool calls remain visible — with no prose, rather than
   * with narration standing in for an answer.
   *
   * The contract is purely client-side and derived from already-present audit
   * rows — no backend "turn_end" signal is required. This intentionally
   * reverses the prior "stream tool calls live" behaviour: the assistant turn
   * is presented atomically once assembled, rather than progressively as
   * audit rows trickle in.
   */
  isComplete: boolean;
}

/**
 * Group an ordered list of audit-grade chat messages into user-visible turns.
 *
 * Rules:
 * - role="user" → emits a `user` turn containing only that message.
 * - role="system" → emits a `system` turn containing only that message.
 *   (System messages render as a centred banner and stay visually distinct;
 *   absorbing them into a surrounding agent turn would hide audit markers
 *   like "Pipeline reverted to version N.")
 * - role="assistant" or role="tool" → extends the current `agent` turn, or
 *   opens a new one if the previous turn was not `agent`. `tool` rows are
 *   normally filtered server-side but are absorbed defensively here so an
 *   accidental leak doesn't produce orphan bubbles.
 */
export function groupIntoTurns(messages: ChatMessage[]): ChatTurn[] {
  const turns: ChatTurn[] = [];
  let current: MutableTurn | null = null;

  for (const message of messages) {
    if (message.role === "user") {
      if (current) turns.push(freeze(current));
      current = null;
      turns.push(freeze(makeStandaloneTurn("user", message)));
      continue;
    }
    if (message.role === "system") {
      if (current) turns.push(freeze(current));
      current = null;
      turns.push(freeze(makeStandaloneTurn("system", message)));
      continue;
    }
    // assistant or tool
    if (current === null) {
      current = makeAgentTurn(message);
    } else {
      extendAgentTurn(current, message);
    }
  }
  if (current) turns.push(freeze(current));
  return turns;
}

interface MutableTurn {
  id: string;
  kind: ChatTurnKind;
  messages: ChatMessage[];
  aggregatedToolCalls: ToolCall[];
  finalContent: string;
  primaryMessage: ChatMessage;
  isComplete: boolean;
}

function makeStandaloneTurn(kind: ChatTurnKind, message: ChatMessage): MutableTurn {
  return {
    id: message.id,
    kind,
    messages: [message],
    aggregatedToolCalls: [],
    finalContent: message.content ?? "",
    primaryMessage: message,
    // user / system turns are standalone audit rows: always complete.
    isComplete: true,
  };
}

function makeAgentTurn(message: ChatMessage): MutableTurn {
  const turn: MutableTurn = {
    id: message.id,
    kind: "agent",
    messages: [message],
    aggregatedToolCalls: [],
    finalContent: "",
    primaryMessage: message,
    // Provisional — flipped to true by absorb() the moment any assistant row
    // in the turn carries non-empty content. See ChatTurn.isComplete docs.
    isComplete: false,
  };
  absorb(turn, message);
  return turn;
}

function extendAgentTurn(turn: MutableTurn, message: ChatMessage): void {
  turn.messages.push(message);
  absorb(turn, message);
}

/**
 * True when this row is the composer's genuine reply to the user, as opposed
 * to narration it wrote on the way to a tool call (elspeth-e074575b6e).
 *
 * The backend guarantees the distinction, so this is a read of existing wire
 * data rather than a heuristic:
 *
 *   - Mid-loop rows are written by `persist_compose_turn_async` together with
 *     the tool calls the model emitted alongside the prose.
 *   - The genuine reply is only produced by `finalize_no_tool_response`, which
 *     the compose loop reaches ONLY when the model emitted no tool calls, and
 *     is persisted by `add_message` with no tool_calls.
 *   - Per-tool audit records are separate role="tool"/"audit" rows; they never
 *     appear as an assistant row carrying tool_calls.
 */
function isGenuineReply(message: ChatMessage): boolean {
  return !(message.tool_calls && message.tool_calls.length > 0);
}

function absorb(turn: MutableTurn, message: ChatMessage): void {
  if (message.tool_calls && message.tool_calls.length > 0) {
    for (const call of message.tool_calls) turn.aggregatedToolCalls.push(call);
  }
  // Track the most recent message overall (acts as the fallback primary when
  // no row in the turn has content).
  turn.primaryMessage = message;
  if (message.content && message.content.length > 0 && isGenuineReply(message)) {
    turn.finalContent = message.content;
    // Atomic-reveal contract: an agent turn is complete once a GENUINE REPLY
    // row carries non-empty content. Keying this on any content at all
    // promoted mid-loop narration ("Submitting the full set_pipeline:") into
    // the answer slot whenever the loop ended without replying — on
    // convergence or timeout, _handle_convergence_error persists the partial
    // state and the tool audit but never an assistant reply row, so the last
    // narration was all that remained. Standalone (user/system) turns are
    // initialised complete by makeStandaloneTurn and never carry tool_calls,
    // so this branch is a no-op for them.
    turn.isComplete = true;
  }
}

function freeze(turn: MutableTurn): ChatTurn {
  // Re-derive primaryMessage now that the turn is closed: prefer the last
  // message with non-empty content, otherwise keep the current pointer (which
  // already tracks the last message in the turn).
  if (turn.kind === "agent") {
    const lastWithContent = lastIndex(
      turn.messages,
      (m) => Boolean(m.content) && isGenuineReply(m),
    );
    if (lastWithContent !== -1) {
      turn.primaryMessage = turn.messages[lastWithContent];
    }
  }
  return turn;
}

function lastIndex<T>(items: T[], pred: (item: T) => boolean): number {
  for (let i = items.length - 1; i >= 0; i--) {
    if (pred(items[i])) return i;
  }
  return -1;
}

/**
 * Build a single representative ChatMessage for the turn, suitable for passing
 * into MessageBubble as `message`. For user/system turns this is the sole
 * underlying message. For agent turns it is the primary message with
 * `tool_calls` overridden to the aggregated set and `content` overridden to
 * the final non-empty content (or "" while the turn is still mid-flight).
 *
 * The underlying audit rows remain untouched on the turn; this is rendering
 * synthesis, not persistence.
 */
export function turnRepresentativeMessage(turn: ChatTurn): ChatMessage {
  if (turn.kind !== "agent") return turn.primaryMessage;
  return {
    ...turn.primaryMessage,
    tool_calls: turn.aggregatedToolCalls.length > 0 ? turn.aggregatedToolCalls : null,
    content: turn.finalContent,
  };
}
