import { describe, expect, it } from "vitest";

import {
  afterConfirmationChatToken,
  completedGuidedChatToken,
} from "./completedChatToken";
import type { GuidedSession, TerminalState, TurnRecord } from "@/types/guided";

const CONFIRMATION_HASH = "c".repeat(64);

const COMPLETED: TerminalState = {
  kind: "completed",
  reason: null,
  pipeline_yaml: "source:\n  plugin: csv\n",
};

const EXITED: TerminalState = {
  kind: "exited_to_freeform",
  reason: "user_pressed_exit",
  pipeline_yaml: null,
};

function wireRecord(overrides: Partial<TurnRecord> = {}): TurnRecord {
  return {
    step: "step_4_wire",
    turn_type: "confirm_wiring",
    payload_hash: "p".repeat(64),
    response_hash: CONFIRMATION_HASH,
    summary: "Wiring confirmed",
    emitter: "server",
    ...overrides,
  };
}

function session(overrides: Partial<GuidedSession> = {}): GuidedSession {
  return {
    step: "step_4_wire",
    history: [wireRecord()],
    terminal: COMPLETED,
    chat_history: [],
    chat_turn_seq: 0,
    reviewed_components: { sources: [], outputs: [] },
    profile: null,
    ...overrides,
  };
}

describe("completedGuidedChatToken", () => {
  it("returns the confirmation hash of a completed session", () => {
    expect(completedGuidedChatToken(session())).toBe(CONFIRMATION_HASH);
  });

  it("reads the LAST record, not the first", () => {
    // A real session's history carries the earlier steps' records ahead of
    // the wire confirmation; the token is the confirmation's own hash.
    const earlier = wireRecord({
      step: "step_1_source",
      turn_type: "single_select",
      response_hash: "a".repeat(64),
    });
    expect(
      completedGuidedChatToken(
        session({ history: [earlier, wireRecord()] }),
      ),
    ).toBe(CONFIRMATION_HASH);
  });

  it("returns null for a live (non-terminal) session", () => {
    expect(completedGuidedChatToken(session({ terminal: null }))).toBeNull();
  });

  it("returns null for an exited_to_freeform terminal", () => {
    // The route refuses an exited session verbatim ("Guided session is
    // already terminal."), so offering a token would only buy a 409.
    expect(completedGuidedChatToken(session({ terminal: EXITED }))).toBeNull();
  });

  it("returns null when the history is empty", () => {
    expect(completedGuidedChatToken(session({ history: [] }))).toBeNull();
  });

  it("returns null when the last record is not the wire confirmation", () => {
    expect(
      completedGuidedChatToken(
        session({
          history: [wireRecord({ turn_type: "propose_pipeline" })],
        }),
      ),
    ).toBeNull();
    expect(
      completedGuidedChatToken(
        session({ history: [wireRecord({ step: "step_3_transforms" })] }),
      ),
    ).toBeNull();
  });

  it("returns null when the last record is unanswered", () => {
    // response_hash null means the confirmation was never recorded — there is
    // no CAS id to chat under, and the backend's own token derivation fails
    // closed on exactly this.
    expect(
      completedGuidedChatToken(
        session({ history: [wireRecord({ response_hash: null })] }),
      ),
    ).toBeNull();
  });

  it("returns null for a null session", () => {
    expect(completedGuidedChatToken(null)).toBeNull();
  });
});

describe("afterConfirmationChatToken", () => {
  it("agrees with the channel token on a completed session", () => {
    // The two derivations must not disagree where both apply: the divider
    // would otherwise open at a turn the store never submitted.
    expect(afterConfirmationChatToken(session())).toBe(CONFIRMATION_HASH);
    expect(afterConfirmationChatToken(session())).toBe(
      completedGuidedChatToken(session()),
    );
  });

  it("survives a settlement that cleared the terminal but kept the transcript", () => {
    // The fork rewind (`terminal=None`, step back to Step 2) and
    // `/guided/reenter` after a content change both do this. Every record of
    // a completed session is answered, so the rewind's truncation arm does
    // not fire: the confirmation record is inherited whole, and the child's
    // inherited post-commit turns still need their boundary.
    const forked = session({ terminal: null, step: "step_2_sink" });

    expect(completedGuidedChatToken(forked)).toBeNull();
    expect(afterConfirmationChatToken(forked)).toBe(CONFIRMATION_HASH);
  });

  it("marks the boundary on an exited replay, whose channel is closed", () => {
    const exited = session({ terminal: EXITED });

    expect(completedGuidedChatToken(exited)).toBeNull();
    expect(afterConfirmationChatToken(exited)).toBe(CONFIRMATION_HASH);
  });

  it("scans backwards past a turn appended after the confirmation", () => {
    // `/guided/reenter` rebuilds a current turn onto the inherited history,
    // so the confirmation is no longer last.
    const rebuilt = wireRecord({
      step: "step_2_sink",
      turn_type: "review_components",
      response_hash: null,
    });

    expect(
      afterConfirmationChatToken(
        session({ terminal: null, history: [wireRecord(), rebuilt] }),
      ),
    ).toBe(CONFIRMATION_HASH);
  });

  it("returns null when no build was ever confirmed", () => {
    expect(
      afterConfirmationChatToken(
        session({
          terminal: null,
          history: [wireRecord({ step: "step_1_source", turn_type: "single_select" })],
        }),
      ),
    ).toBeNull();
    expect(afterConfirmationChatToken(session({ history: [] }))).toBeNull();
    expect(afterConfirmationChatToken(null)).toBeNull();
  });

  it("returns null while the wire turn is still unanswered", () => {
    // The confirmation record exists but nothing was committed under it, so
    // there is no boundary — and no token any turn could carry.
    expect(
      afterConfirmationChatToken(
        session({ terminal: null, history: [wireRecord({ response_hash: null })] }),
      ),
    ).toBeNull();
  });
});
