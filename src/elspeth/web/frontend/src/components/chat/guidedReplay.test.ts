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

  // ── The seeded goal pair (goal-first, elspeth-378cfa0e18) ─────────────────
  //
  // A started or converted guided session's chat_history OPENS with the goal
  // (a user turn) and one assistant acknowledgement. On exit_to_freeform that
  // history is replayed above the freeform transcript, so the goal has to obey
  // the same consumption rule as any other guided send — otherwise the very
  // first line of the replayed conversation is the one that renders twice.

  it("consumes exactly one freeform copy of the seeded goal", () => {
    const goal = "Summarise each page and save the results as JSON.";
    const kept = dedupeGuidedUserMessages(
      [message("m1", "user", goal), message("m2", "user", "Now add a sink.")],
      [
        guidedTurn(0, "user", goal),
        guidedTurn(
          1,
          "assistant",
          "Goal saved. The planner will build from it once the source and output are reviewed. First, the source: where does the data come from?",
        ),
      ],
    );
    expect(kept).toEqual([message("m2", "user", "Now add a sink.")]);
  });

  it("leaves a re-stated goal visible after graduation (consumption, not blanket suppression)", () => {
    // If the user says the same sentence again in freeform, that second row is
    // a real message and must render: the guided-phase copy was already spent
    // by the first row.
    const goal = "Summarise each page and save the results as JSON.";
    const rows = [message("m1", "user", goal), message("m2", "user", goal)];
    expect(dedupeGuidedUserMessages(rows, [guidedTurn(0, "user", goal)])).toEqual([
      rows[1],
    ]);
  });

  it("the goal acknowledgement consumes nothing — it is an assistant turn", () => {
    const ack =
      "Goal saved. The planner will build from it once the source and output are reviewed. First, the source: where does the data come from?";
    const rows = [message("m1", "user", ack)];
    expect(dedupeGuidedUserMessages(rows, [guidedTurn(1, "assistant", ack)])).toEqual(
      rows,
    );
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
