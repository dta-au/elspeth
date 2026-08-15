import { describe, expect, it } from "vitest";
import type { ChatMessage } from "@/types/api";
import type { ChatTurn as GuidedWireTurn } from "@/types/guided";
import { dedupeGuidedUserMessages } from "./guidedReplay";

function message(
  id: string,
  role: ChatMessage["role"],
  content: string,
): ChatMessage {
  return {
    id,
    session_id: "session-1",
    role,
    content,
    tool_calls: null,
    created_at: "2026-08-15T05:00:00Z",
  };
}

function guidedTurn(
  seq: number,
  role: "user" | "assistant",
  content: string,
): GuidedWireTurn {
  return {
    role,
    content,
    seq,
    step: "step_1_source",
    ts_iso: "2026-08-15T05:00:00Z",
    assistant_message_kind: role === "assistant" ? "assistant" : null,
    synthetic_failure_reason: null,
    turn_token: role === "user" ? "a".repeat(64) : null,
  };
}

describe("dedupeGuidedUserMessages", () => {
  it("drops a user row whose content a guided user turn already carries", () => {
    const kept = dedupeGuidedUserMessages(
      [message("m1", "user", "Please create a CSV source.")],
      [
        guidedTurn(0, "user", "Please create a CSV source."),
        guidedTurn(1, "assistant", "Done — CSV source added."),
      ],
    );
    expect(kept).toEqual([]);
  });

  it("matches on trimmed content (the two stores differ in surrounding whitespace)", () => {
    const kept = dedupeGuidedUserMessages(
      [message("m1", "user", "  Save results to JSON.\n")],
      [guidedTurn(0, "user", "Save results to JSON.")],
    );
    expect(kept).toEqual([]);
  });

  it("consumes each guided turn once so a repeat sent after graduation still renders", () => {
    const guidedPhaseRow = message("m1", "user", "Run it again.");
    const postGraduationRow = message("m2", "user", "Run it again.");
    const kept = dedupeGuidedUserMessages(
      [guidedPhaseRow, postGraduationRow],
      [guidedTurn(0, "user", "Run it again.")],
    );
    // In-order consumption: the guided-phase duplicate (first) is the one
    // cancelled; the later freeform re-send survives.
    expect(kept).toEqual([postGraduationRow]);
  });

  it("never touches non-user rows even on content collision", () => {
    const assistantRow = message("m1", "assistant", "Echoed text");
    const kept = dedupeGuidedUserMessages(
      [assistantRow],
      [guidedTurn(0, "user", "Echoed text")],
    );
    expect(kept).toEqual([assistantRow]);
  });

  it("passes everything through when the guided history has no user turns", () => {
    const rows = [message("m1", "user", "Hello")];
    expect(
      dedupeGuidedUserMessages(rows, [guidedTurn(0, "assistant", "Hello")]),
    ).toEqual(rows);
    expect(dedupeGuidedUserMessages(rows, [])).toEqual(rows);
  });

  it("keeps user rows with no matching guided turn (ordinary freeform chat)", () => {
    const rows = [
      message("m1", "user", "Please create a CSV source."),
      message("m2", "user", "Now add a database sink."),
    ];
    const kept = dedupeGuidedUserMessages(rows, [
      guidedTurn(0, "user", "Please create a CSV source."),
    ]);
    expect(kept).toEqual([rows[1]]);
  });
});
