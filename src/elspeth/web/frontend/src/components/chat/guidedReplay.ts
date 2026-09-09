// src/components/chat/guidedReplay.ts
//
// Dedupe rule for rendering a terminal guided session's conversation in the
// freeform transcript (elspeth-2554bff719).
//
// Guided mode persists the assistant conversation ONLY in the guided state
// machine's chat_history; chat_messages carries no assistant rows for those
// turns. The user's guided sends, however, land in BOTH stores: the guided
// chat path both appends a chat_history user turn and writes an ordinary
// chat_messages user row. When the freeform surface replays the guided
// chat_history above its own transcript, each of those user rows would
// otherwise render twice.
//
// There is no shared key between the stores (chat_messages has no
// turn_token column), so the rule matches on trimmed content with
// CONSUMPTION semantics: each guided user turn cancels at most ONE freeform
// user row, in transcript order. Consumption is what keeps a genuinely
// repeated message honest — if the user re-sends the same text after
// graduating to freeform, the guided-phase copy is consumed by the first
// (guided-phase) row and the later row still renders.

import type { ChatMessage } from "@/types/api";
import type { ChatTurn as GuidedWireTurn } from "@/types/guided";

/**
 * Filter `messages` for rendering below a replayed guided chat_history:
 * drop each user row already represented by a guided user turn (matched on
 * trimmed content, each guided turn consumed at most once, in order).
 * Non-user rows always pass through.
 */
export function dedupeGuidedUserMessages(
  messages: ChatMessage[],
  guidedHistory: GuidedWireTurn[],
): ChatMessage[] {
  if (guidedHistory.length === 0) {
    return messages;
  }
  const budget = new Map<string, number>();
  for (const turn of guidedHistory) {
    if (turn.role !== "user") {
      continue;
    }
    const key = turn.content.trim();
    budget.set(key, (budget.get(key) ?? 0) + 1);
  }
  if (budget.size === 0) {
    return messages;
  }
  return messages.filter((message) => {
    if (message.role !== "user") {
      return true;
    }
    const key = message.content.trim();
    const remaining = budget.get(key) ?? 0;
    if (remaining === 0) {
      return true;
    }
    budget.set(key, remaining - 1);
    return false;
  });
}
