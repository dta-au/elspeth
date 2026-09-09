// src/components/chat/guided/completedChatToken.ts
//
// The ONE frontend authority for "which turn_token does a completed guided
// session chat under" (elspeth-986801d218 / IA-6).
//
// After Confirm wiring the guided session is terminal — there is no
// unanswered turn and therefore no `next_turn.turn_token` — but the chat
// channel stays open so the user can ask about the pipeline they just built.
// The backend admits that request only when the submitted token equals the
// CONFIRMATION hash: `guided_session.history[-1].response_hash`, the CAS id of
// the `confirm_wiring` response record (`guided_completed_chat_token` in
// `web/sessions/guided_replay.py`). No wire field was added for this — the
// token is derived from the session the client already holds.
//
// PURE LEAF by contract: it imports ONLY `types/guided`, so both the store
// (`sessionStore.chatGuided`) and the view (`ChatPanel`, `GuidedChatHistory`'s
// "After confirmation" divider) can bind to the same derivation with no cycle.
// A second copy of this rule anywhere is the defect it exists to prevent: the
// store would post one token while the transcript drew its divider from
// another.
//
// Fails CLOSED. Every condition below is a fact the backend re-derives before
// it admits the chat, so a null here is the honest client answer ("this
// session has no completed-chat channel") rather than a guess that draws a
// 409: the terminal must be `completed` (an `exited_to_freeform` session is
// refused verbatim by the route), the last history record must be the
// answered `step_4_wire` / `confirm_wiring` record, and its `response_hash`
// must be present.

import type { GuidedSession } from "@/types/guided";

/**
 * The turn token a COMPLETED guided session's chat must be submitted under,
 * or `null` when this session has no completed-chat channel.
 *
 * Null is returned for a live session, an `exited_to_freeform` terminal, an
 * empty history, a last record that is not the wire-confirm record, and an
 * unanswered last record (`response_hash === null`).
 */
export function completedGuidedChatToken(
  session: GuidedSession | null,
): string | null {
  if (session === null) return null;
  if (session.terminal === null) return null;
  if (session.terminal.kind !== "completed") return null;
  // Index form, not `.at(-1)`: the app tsconfig's lib predates ES2022.
  const history = session.history;
  if (history.length === 0) return null;
  const last = history[history.length - 1];
  if (last.step !== "step_4_wire") return null;
  if (last.turn_type !== "confirm_wiring") return null;
  return last.response_hash;
}

/**
 * The token every POST-CONFIRMATION chat turn in this transcript was submitted
 * under, or `null` when the session never confirmed a build.
 *
 * The TRANSCRIPT question, not the channel question — and they are different
 * on more sessions than they look. A settlement can clear `terminal` while
 * keeping `chat_history` and the answered `confirm_wiring` record intact:
 * a fork rewinds the child to Step 2 with `terminal=None`
 * (`sessions/service.py` — every record of a completed session is answered, so
 * the rewind's truncation arm does not fire and the confirmation record is
 * inherited whole), and `/guided/reenter` on content that changed under the
 * exit does the same. `exited_to_freeform` keeps its terminal but closes the
 * channel. On all three `completedGuidedChatToken` correctly returns null —
 * that session cannot chat under the confirmation hash any more — yet their
 * transcripts still carry post-commit turns that need the "After confirmation"
 * boundary, and a reenter can append a rebuilt turn AFTER the confirmation
 * record, which is why this scans backwards instead of reading `history[-1]`.
 *
 * Never use this as the request token: posting it on a session the channel has
 * moved off would submit a dead occurrence. `completedGuidedChatToken` is the
 * only authority for what the client may send.
 */
export function afterConfirmationChatToken(
  session: GuidedSession | null,
): string | null {
  if (session === null) return null;
  const history = session.history;
  for (let index = history.length - 1; index >= 0; index -= 1) {
    const record = history[index];
    if (record.step !== "step_4_wire") continue;
    if (record.turn_type !== "confirm_wiring") continue;
    // An unanswered confirmation is the wire turn still on screen: nothing
    // was committed under it, so there is no boundary to draw.
    return record.response_hash;
  }
  return null;
}
