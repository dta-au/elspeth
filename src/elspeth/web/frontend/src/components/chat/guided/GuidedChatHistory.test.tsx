// ============================================================================
// GuidedChatHistory -- per-step chat log regression coverage.
//
// One rendering idiom (the tutorial-workspace bubbles, promoted to the only
// guided transcript when the workspace became the one guided layout —
// the flat <ol> list died with the pre-workspace flat layout):
//
//   1. Empty state: returns null (no DOM contribution) when chat_history
//      is [].
//   2. Live region: role="log", aria-live="polite",
//      aria-relevant="additions" — new chat turns are announced to screen
//      readers when appended.  Distinct from ChatPanel's wizard log region;
//      the two coexist.
//   3. Rows compose freeform's CSS classes: .message-row--user (right) /
//      .message-row--assistant (left); bubbles compose .bubble-user /
//      .bubble-assistant + .message-bubble-content(--user).
//   4. Assistant content renders through MarkdownRenderer (**bold** →
//      <strong>); user content stays PLAIN TEXT (the tutorial's locked
//      prompt's authored newlines/URLs must survive pre-wrap, and literal
//      markdown characters must not be re-rendered as formatting).
//   5. sr-only author prefixes use freeform's register: "You said:" /
//      "ELSPETH said:".
//   6. Stage dividers: one per step-change boundary (including the
//      transcript start) — .bubble-system--stage rows with accessible text
//      "<Label> stage", NOT aria-hidden, no per-turn step badges. "Output"
//      vocabulary, "Sink" absent (shared stepLabels map).
//   7. Out-of-order seq defense: rendering sorts by `seq` rather than array
//      index, so a backend returning entries out of order still produces a
//      stable, monotonic chat log.  Slice 5 guarantees monotonic seq; the
//      sort is belt-and-braces against future bugs.
//   8. The flat-list markup is GONE: no .guided-chat-history* classes leak
//      out of history.
//
// Source of truth:
//   - types/guided.ts ChatTurn (role / content / seq / step / ts_iso)
//   - state_machine.py GuidedSession.chat_history (server-authoritative)
//   - stepLabels.ts GUIDED_STEP_LABELS (shared wizard-step vocabulary)
//   - MessageBubble.tsx / chat.css (bubble idiom + sr-only author register)
// ============================================================================

import { describe, it, expect, vi } from "vitest";
import { fireEvent, render, screen } from "@testing-library/react";
import { GuidedChatHistory } from "./GuidedChatHistory";
import type { ChatTurn } from "@/types/guided";

// ── Fixtures ──────────────────────────────────────────────────────────────────

const TURN_USER: ChatTurn = {
  role: "user",
  content: "what columns are in this CSV?",
  seq: 0,
  step: "step_1_source",
  ts_iso: "2026-05-13T12:00:00+00:00",
  assistant_message_kind: null,
  synthetic_failure_reason: null,
  turn_token: null,
};

const TURN_ASSISTANT: ChatTurn = {
  role: "assistant",
  content: "The CSV has price, quantity, and timestamp columns.",
  seq: 1,
  step: "step_1_source",
  ts_iso: "2026-05-13T12:00:00+00:00",
  assistant_message_kind: "assistant",
  synthetic_failure_reason: null,
  turn_token: null,
};

const TWO_TURNS: ChatTurn[] = [TURN_USER, TURN_ASSISTANT];

/** Four turns spanning two steps → exactly one step-change boundary plus the
    transcript-start boundary. */
const CROSS_STEP: ChatTurn[] = [
  { ...TURN_USER, seq: 0, step: "step_1_source" },
  { ...TURN_ASSISTANT, seq: 1, step: "step_1_source" },
  { ...TURN_USER, seq: 2, step: "step_2_sink", content: "what about outputs?" },
  { ...TURN_ASSISTANT, seq: 3, step: "step_2_sink", content: "ack" },
];

// ── 1. Empty state ───────────────────────────────────────────────────────────

describe("GuidedChatHistory empty state", () => {
  it("returns null and contributes no DOM when chat_history is []", () => {
    const { container } = render(<GuidedChatHistory chatHistory={[]} />);

    expect(screen.queryByRole("log")).not.toBeInTheDocument();
    expect(container).toBeEmptyDOMElement();
  });
});

// ── 2. Live-region contract ──────────────────────────────────────────────────

describe("GuidedChatHistory live region", () => {
  it("renders a role=log live region with aria-live=polite + aria-relevant=additions", () => {
    render(<GuidedChatHistory chatHistory={TWO_TURNS} />);

    const log = screen.getByRole("log", { name: "Step chat history" });
    expect(log).toBeInTheDocument();
    expect(log).toHaveAttribute("aria-live", "polite");
    expect(log).toHaveAttribute("aria-relevant", "additions");
  });

  it("shows the literal content of each turn", () => {
    render(<GuidedChatHistory chatHistory={TWO_TURNS} />);

    expect(screen.getByText("what columns are in this CSV?")).toBeInTheDocument();
    expect(
      screen.getByText("The CSV has price, quantity, and timestamp columns."),
    ).toBeInTheDocument();
  });

  it("replay variant renders a static labelled group, not a nested live log (elspeth-2554bff719)", () => {
    // The freeform transcript replays a TERMINAL guided session's history
    // inside its own role=log region; the settled replay must not nest a
    // second live region there. Rows are unchanged — same bubbles, content.
    render(<GuidedChatHistory chatHistory={TWO_TURNS} replay />);

    const group = screen.getByRole("group", {
      name: "Guided build conversation",
    });
    expect(group).not.toHaveAttribute("aria-live");
    expect(screen.queryByRole("log")).not.toBeInTheDocument();
    expect(group).toHaveTextContent(
      "The CSV has price, quantity, and timestamp columns.",
    );
  });
});

// ── 3. Freeform bubble idiom ─────────────────────────────────────────────────

describe("GuidedChatHistory bubble markup", () => {
  it("composes freeform's row + bubble classes per role", () => {
    const { container } = render(<GuidedChatHistory chatHistory={TWO_TURNS} />);

    const userRow = container.querySelector(".message-row--user");
    const assistantRow = container.querySelector(".message-row--assistant");
    expect(userRow).not.toBeNull();
    expect(assistantRow).not.toBeNull();

    // User bubble: .bubble-user + the --user content cap (right-aligned,
    // pre-wrap — preserves the tutorial locked prompt's authored newlines).
    const userBubble = userRow!.querySelector(".bubble");
    expect(userBubble).not.toBeNull();
    expect(userBubble!.classList.contains("bubble-user")).toBe(true);
    expect(userBubble!.classList.contains("message-bubble-content")).toBe(true);
    expect(userBubble!.classList.contains("message-bubble-content--user")).toBe(true);

    // Assistant bubble: .bubble-assistant, content cap without the --user
    // pre-wrap modifier (markdown structures its own breaks).
    const assistantBubble = assistantRow!.querySelector(".bubble");
    expect(assistantBubble).not.toBeNull();
    expect(assistantBubble!.classList.contains("bubble-assistant")).toBe(true);
    expect(assistantBubble!.classList.contains("message-bubble-content")).toBe(true);
    expect(assistantBubble!.classList.contains("message-bubble-content--user")).toBe(false);
  });

  it("renders no flat-list markup (died with the pre-workspace layout)", () => {
    const { container } = render(<GuidedChatHistory chatHistory={TWO_TURNS} />);

    expect(container.querySelector(".guided-chat-history")).toBeNull();
    expect(container.querySelector(".guided-chat-history-item")).toBeNull();
    // Per-turn step badges are gone too — stage dividers replace them.
    expect(container.querySelector(".guided-chat-history-step")).toBeNull();
  });
});

// ── 4. Markdown: assistant only ──────────────────────────────────────────────

describe("GuidedChatHistory markdown", () => {
  it("renders assistant content through markdown but keeps user content literal", () => {
    const turns: ChatTurn[] = [
      { ...TURN_USER, content: "make **this** a pipeline" },
      { ...TURN_ASSISTANT, content: "I built **three** transforms." },
    ];
    const { container } = render(<GuidedChatHistory chatHistory={turns} />);

    // Assistant: **three** became <strong> inside a .markdown-body wrapper
    // (the wrapper also carries chat.css's white-space reset — load-bearing).
    const assistantBubble = container.querySelector(".bubble-assistant");
    expect(assistantBubble!.querySelector(".markdown-body")).not.toBeNull();
    const strong = assistantBubble!.querySelector("strong");
    expect(strong).not.toBeNull();
    expect(strong!.textContent).toBe("three");

    // User: literal text, asterisks intact, no markdown rendering.
    const userBubble = container.querySelector(".bubble-user");
    expect(userBubble!.querySelector("strong")).toBeNull();
    expect(userBubble!.querySelector(".markdown-body")).toBeNull();
    expect(userBubble!.textContent).toContain("make **this** a pipeline");
  });
});

// ── 5. sr-only author prefixes (freeform register) ───────────────────────────

describe("GuidedChatHistory SR prefixes", () => {
  it("prefixes each bubble with freeform's sr-only author register", () => {
    render(<GuidedChatHistory chatHistory={TWO_TURNS} />);

    const userPrefix = screen.getByText("You said:", { exact: false });
    expect(userPrefix).toBeInTheDocument();
    expect(userPrefix).toHaveClass("sr-only");

    // Freeform's register is "ELSPETH said:", not the dead flat list's
    // "Assistant said:".
    const assistantPrefix = screen.getByText("ELSPETH said:", { exact: false });
    expect(assistantPrefix).toBeInTheDocument();
    expect(assistantPrefix).toHaveClass("sr-only");
    expect(screen.queryByText("Assistant said:", { exact: false })).toBeNull();
  });
});

// ── 6. Stage dividers ────────────────────────────────────────────────────────

describe("GuidedChatHistory stage dividers", () => {
  it("renders one divider per step-change boundary, in seq order", () => {
    const { container } = render(<GuidedChatHistory chatHistory={CROSS_STEP} />);

    // Two boundaries: transcript start (Source stage) + the step_1→step_2
    // change (Output stage). NOT one per turn.
    const dividers = container.querySelectorAll(".bubble-system--stage");
    expect(dividers).toHaveLength(2);
    expect(dividers[0].textContent).toBe("Source stage");
    expect(dividers[1].textContent).toBe("Output stage");

    // Divider rows reuse the centred system-row visual.
    for (const divider of dividers) {
      const row = divider.closest(".message-row");
      expect(row).not.toBeNull();
      expect(row!.classList.contains("message-row--system")).toBe(true);
      // Announced once via the log's additions semantics — never aria-hidden.
      expect(divider.getAttribute("aria-hidden")).toBeNull();
      expect(row!.getAttribute("aria-hidden")).toBeNull();
    }

    // The " stage" suffix is sr-only; the visible label is just the step name.
    const suffix = dividers[0].querySelector(".sr-only");
    expect(suffix).not.toBeNull();
    expect(suffix!.textContent).toBe(" stage");
  });

  it("uses the shared 'Output' vocabulary — 'Sink' never appears", () => {
    render(<GuidedChatHistory chatHistory={CROSS_STEP} />);

    expect(screen.getByText(/Output/)).toBeInTheDocument();
    expect(screen.queryByText(/Sink/)).toBeNull();
  });

  it("does not repeat the divider while consecutive turns share a step", () => {
    const { container } = render(<GuidedChatHistory chatHistory={TWO_TURNS} />);

    // Single-step history → exactly one divider (the transcript start).
    const dividers = container.querySelectorAll(".bubble-system--stage");
    expect(dividers).toHaveLength(1);
    expect(dividers[0].textContent).toBe("Source stage");
  });
});

// ── 7. seq order on bubble rows ──────────────────────────────────────────────

describe("GuidedChatHistory seq ordering", () => {
  it("renders turn rows in seq order even when the input array is shuffled", () => {
    const shuffled: ChatTurn[] = [TURN_ASSISTANT, TURN_USER]; // seq 1, then seq 0
    const { container } = render(<GuidedChatHistory chatHistory={shuffled} />);

    const rows = container.querySelectorAll("[data-seq]");
    expect(rows).toHaveLength(2);
    expect(rows[0].getAttribute("data-seq")).toBe("0");
    expect(rows[1].getAttribute("data-seq")).toBe("1");
    // The lower-seq row is the user's turn.
    expect(rows[0].classList.contains("message-row--user")).toBe(true);
    expect(rows[1].classList.contains("message-row--assistant")).toBe(true);
  });
});

// ── 8. Synthetic-failure turns (C-2) ─────────────────────────────────────────

const TURN_SYNTHETIC_FAILURE: ChatTurn = {
  role: "assistant",
  content: "I'm unavailable right now; you can still use the wizard controls.",
  seq: 1,
  step: "step_1_source",
  ts_iso: "2026-05-13T12:00:00+00:00",
  assistant_message_kind: "synthetic_failure",
  synthetic_failure_reason: "unavailable",
  turn_token: null,
};

describe("GuidedChatHistory synthetic-failure turns", () => {
  it("never renders the ELSPETH-said assistant bubble for a synthetic-failure turn", () => {
    const { container } = render(
      <GuidedChatHistory chatHistory={[TURN_USER, TURN_SYNTHETIC_FAILURE]} />,
    );

    // No "ELSPETH said:" prefix anywhere — the synthetic turn must not read
    // as a real assistant reply.
    expect(screen.queryByText("ELSPETH said:", { exact: false })).toBeNull();
    expect(container.querySelector(".bubble-assistant")).toBeNull();
  });

  it("renders a distinct error bubble carrying the server's message, without an assertive alert role", () => {
    const { container } = render(
      <GuidedChatHistory chatHistory={[TURN_SYNTHETIC_FAILURE]} />,
    );

    // The error bubble is visually and semantically distinct (bubble-error +
    // sr-only "Error:" prefix), but it is NOT role="alert": these turns are
    // persisted history, so an assertive alert would re-announce every past
    // failure on each remount. Live announcement of a new failure is the
    // parent transcript log's job (aria-live="polite").
    expect(screen.queryByRole("alert")).toBeNull();
    const bubble = container.querySelector(".bubble-error");
    expect(bubble).not.toBeNull();
    expect(bubble).toHaveTextContent(
      "I'm unavailable right now; you can still use the wizard controls.",
    );
    expect(bubble).toHaveTextContent("Error:");
  });

  it("renders a non-applying source result as a typed synthetic failure", () => {
    const notApplied: ChatTurn = {
      ...TURN_SYNTHETIC_FAILURE,
      content: "I did not apply generated source content.",
      synthetic_failure_reason: "not_applied",
      turn_token: null,
    };
    const { container } = render(<GuidedChatHistory chatHistory={[notApplied]} />);

    expect(container.querySelector(".bubble-error")).toHaveTextContent(
      "I did not apply generated source content.",
    );
  });

  it("omits the Retry button when no onRetrySyntheticFailure handler is supplied", () => {
    render(<GuidedChatHistory chatHistory={[TURN_SYNTHETIC_FAILURE]} />);

    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("Retry calls the handler with the failed turn", () => {
    const onRetry = vi.fn();
    render(
      <GuidedChatHistory
        chatHistory={[TURN_SYNTHETIC_FAILURE]}
        onRetrySyntheticFailure={onRetry}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Retry" }));
    expect(onRetry).toHaveBeenCalledTimes(1);
    expect(onRetry).toHaveBeenCalledWith(TURN_SYNTHETIC_FAILURE);
  });

  it("withholds Retry on a synthetic failure the conversation has moved past", () => {
    // Tutorial run 19 (session 921491db): a step-1 provider failure was
    // recovered (re-send succeeded), but the recovered failure turn kept a
    // live Retry for the rest of the walk. Clicking it re-sends the turn
    // preceding the FAILURE — the stale step-1 prompt — at whatever step is
    // now current, which the sink stage answered with an advisory ~22 times
    // until the walk deadline. Retry is a recovery for the transcript's
    // LAST turn only; once any later turn exists the conversation has moved
    // on and re-sending the stale prompt is never the designed recovery.
    const recovery: ChatTurn = {
      ...TURN_USER,
      seq: 2,
      content: "Create the source for this pipeline.",
    };
    const success: ChatTurn = {
      role: "assistant",
      content: "Created a CSV source with three rows.",
      seq: 3,
      step: "step_1_source",
      ts_iso: "2026-05-13T12:01:00+00:00",
      assistant_message_kind: "assistant",
      synthetic_failure_reason: null,
      turn_token: null,
    };
    render(
      <GuidedChatHistory
        chatHistory={[TURN_USER, TURN_SYNTHETIC_FAILURE, recovery, success]}
        onRetrySyntheticFailure={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it("suppresses Retry for a deterministic not_applied failure (inv-f1 D4)", () => {
    // "not_applied" means the user's input was processed and its application
    // rejected (config invalid, upload mismatch, transition rejected):
    // re-sending the SAME message is a guaranteed dead end, and the turn's
    // copy already directs the real next step.
    const notApplied: ChatTurn = {
      ...TURN_SYNTHETIC_FAILURE,
      content: "I couldn't apply that configuration, so I didn't change your pipeline.",
      synthetic_failure_reason: "not_applied",
      turn_token: null,
    };
    render(
      <GuidedChatHistory
        chatHistory={[notApplied]}
        onRetrySyntheticFailure={vi.fn()}
      />,
    );

    expect(screen.queryByRole("button", { name: "Retry" })).toBeNull();
  });

  it.each(["unavailable", "quality_guard", "model_defect"] as const)(
    "keeps Retry for a %s failure — retry is the designed remedy",
    (reason) => {
      const turn: ChatTurn = {
        ...TURN_SYNTHETIC_FAILURE,
        synthetic_failure_reason: reason,
        turn_token: null,
      };
      render(
        <GuidedChatHistory chatHistory={[turn]} onRetrySyntheticFailure={vi.fn()} />,
      );

      expect(screen.getByRole("button", { name: "Retry" })).toBeEnabled();
    },
  );

  it("disables Retry while retryDisabled is set (no race with an in-flight resend)", () => {
    render(
      <GuidedChatHistory
        chatHistory={[TURN_SYNTHETIC_FAILURE]}
        onRetrySyntheticFailure={vi.fn()}
        retryDisabled
      />,
    );

    expect(screen.getByRole("button", { name: "Retry" })).toBeDisabled();
  });

  it("still emits the step-change stage divider around a synthetic-failure turn", () => {
    const { container } = render(
      <GuidedChatHistory chatHistory={[TURN_SYNTHETIC_FAILURE]} />,
    );

    expect(container.querySelector(".bubble-system--stage")).not.toBeNull();
  });
});

// ── "After confirmation" divider (elspeth-986801d218) ────────────────────────
//
// A completed guided session keeps its conversation, and every post-commit
// question is submitted under ONE token — the confirmation hash. The divider
// marks where the build ended.
//
// Discrimination: the boundary is the TOKEN, never the step. Post-commit
// turns are persisted with step="step_4_wire", identical to the pre-commit
// wire turns, so the stage divider cannot mark it — a test that only seeded
// post-commit turns would pass against an implementation that keyed on the
// step and shipped a divider before every pre-commit wire turn too.
describe("GuidedChatHistory — After confirmation divider", () => {
  const CONFIRMATION_HASH = "c".repeat(64);
  const LIVE_TURN_TOKEN = "a".repeat(64);

  function wireTurn(overrides: Partial<ChatTurn>): ChatTurn {
    return {
      role: "user",
      content: "…",
      seq: 0,
      step: "step_4_wire",
      ts_iso: "2026-09-03T12:00:00+00:00",
      assistant_message_kind: null,
      synthetic_failure_reason: null,
      turn_token: null,
      ...overrides,
    };
  }

  /** Pre-commit wire question, post-commit question, and its reply. */
  const HISTORY: ChatTurn[] = [
    wireTurn({ seq: 0, content: "why is node-2 here?", turn_token: LIVE_TURN_TOKEN }),
    wireTurn({ seq: 1, role: "assistant", content: "It reshapes rows.", assistant_message_kind: "assistant" }),
    wireTurn({ seq: 2, content: "what does node-2 do?", turn_token: CONFIRMATION_HASH }),
    wireTurn({ seq: 3, role: "assistant", content: "It calls the model once per row.", assistant_message_kind: "assistant" }),
    wireTurn({ seq: 4, content: "and the output?", turn_token: CONFIRMATION_HASH }),
  ];

  function dividerLabels(container: HTMLElement): string[] {
    return Array.from(
      container.querySelectorAll(".bubble-system--stage"),
    ).map((node) => node.textContent ?? "");
  }

  it("opens the divider at the FIRST turn carrying the confirmation token", () => {
    const { container } = render(
      <GuidedChatHistory
        chatHistory={HISTORY}
        afterConfirmationToken={CONFIRMATION_HASH}
      />,
    );

    const labels = dividerLabels(container);
    // Stage divider for the wire step, then exactly one boundary divider.
    expect(labels.filter((l) => l.includes("After confirmation"))).toHaveLength(1);
    expect(screen.getByText("After confirmation")).toBeInTheDocument();
  });

  it("places the divider AFTER the pre-confirmation turns and BEFORE the first post-commit one", () => {
    const { container } = render(
      <GuidedChatHistory
        chatHistory={HISTORY}
        afterConfirmationToken={CONFIRMATION_HASH}
      />,
    );

    const rows = Array.from(container.querySelectorAll(".message-row"));
    const dividerIndex = rows.findIndex(
      (row) => row.textContent === "After confirmation",
    );
    const preCommitIndex = rows.findIndex((row) =>
      (row.textContent ?? "").includes("why is node-2 here?"),
    );
    const postCommitIndex = rows.findIndex((row) =>
      (row.textContent ?? "").includes("what does node-2 do?"),
    );
    expect(dividerIndex).toBeGreaterThan(preCommitIndex);
    expect(dividerIndex).toBeLessThan(postCommitIndex);
  });

  it("does not repeat the divider on later post-commit turns", () => {
    const { container } = render(
      <GuidedChatHistory
        chatHistory={HISTORY}
        afterConfirmationToken={CONFIRMATION_HASH}
      />,
    );

    // Two user turns carry the confirmation hash (seq 2 and seq 4); the
    // divider is a one-shot boundary, not a per-turn badge.
    expect(
      dividerLabels(container).filter((l) => l.includes("After confirmation")),
    ).toHaveLength(1);
  });

  it("renders no divider on a live session (token null) even at the wire step", () => {
    const { container } = render(<GuidedChatHistory chatHistory={HISTORY} />);

    expect(
      dividerLabels(container).some((l) => l.includes("After confirmation")),
    ).toBe(false);
  });

  it("renders no divider when no turn carries the token", () => {
    const { container } = render(
      <GuidedChatHistory
        chatHistory={[HISTORY[0], HISTORY[1]]}
        afterConfirmationToken={CONFIRMATION_HASH}
      />,
    );

    expect(
      dividerLabels(container).some((l) => l.includes("After confirmation")),
    ).toBe(false);
  });

  it("never opens on an ASSISTANT turn that happens to carry the token", () => {
    // Assistant turns carry a null token on the wire; this pins the role
    // guard so a future shape change cannot draw the boundary one row early.
    const { container } = render(
      <GuidedChatHistory
        chatHistory={[
          wireTurn({
            seq: 0,
            role: "assistant",
            content: "reply",
            assistant_message_kind: "assistant",
            turn_token: CONFIRMATION_HASH,
          }),
        ]}
        afterConfirmationToken={CONFIRMATION_HASH}
      />,
    );

    expect(
      dividerLabels(container).some((l) => l.includes("After confirmation")),
    ).toBe(false);
  });

  it("applies in replay mode too", () => {
    const { container } = render(
      <GuidedChatHistory
        chatHistory={HISTORY}
        afterConfirmationToken={CONFIRMATION_HASH}
        replay
      />,
    );

    expect(screen.getByRole("group", { name: "Guided build conversation" })).toBeInTheDocument();
    expect(
      dividerLabels(container).filter((l) => l.includes("After confirmation")),
    ).toHaveLength(1);
  });
});

// ── 9. The seeded goal pair (goal-first, elspeth-378cfa0e18) ─────────────────

describe("GuidedChatHistory seeded goal opening", () => {
  // A started or converted session's transcript now OPENS with two seeded
  // turns: the goal the user stated, and one server line acknowledging it and
  // handing off to the source question. Both are stamped step_1_source, so
  // they must ride under the existing "Source stage" divider like any other
  // step-1 turns. The failure this pins is a SECOND divider — a "goal stage"
  // that does not exist in the step vocabulary — or the pair rendering out of
  // order, which would read as the assistant answering before being asked.
  const SEEDED_GOAL: ChatTurn[] = [
    {
      ...TURN_USER,
      seq: 0,
      step: "step_1_source",
      content: "Summarise each page and save the results as JSON.",
    },
    {
      ...TURN_ASSISTANT,
      seq: 1,
      step: "step_1_source",
      content:
        "Goal saved. The planner will build from it once the source and output are reviewed. First, the source: where does the data come from?",
    },
  ];

  it("renders the pair under ONE Source stage divider, in seq order", () => {
    const { container } = render(<GuidedChatHistory chatHistory={SEEDED_GOAL} />);

    const dividers = container.querySelectorAll(".bubble-system--stage");
    expect(dividers).toHaveLength(1);
    expect(dividers[0].textContent).toBe("Source stage");

    const rows = Array.from(container.querySelectorAll(".message-row"));
    const goalRow = rows.findIndex((row) =>
      row.textContent?.includes("Summarise each page"),
    );
    const ackRow = rows.findIndex((row) =>
      row.textContent?.includes("Goal saved."),
    );
    expect(goalRow).toBeGreaterThanOrEqual(0);
    expect(ackRow).toBeGreaterThan(goalRow);
  });

  it("keeps the goal on the user side and the acknowledgement on ELSPETH's", () => {
    const { container } = render(<GuidedChatHistory chatHistory={SEEDED_GOAL} />);

    const goalRow = Array.from(container.querySelectorAll(".message-row")).find(
      (row) => row.textContent?.includes("Summarise each page"),
    );
    expect(goalRow?.classList.contains("message-row--user")).toBe(true);
    const ackRow = Array.from(container.querySelectorAll(".message-row")).find(
      (row) => row.textContent?.includes("Goal saved."),
    );
    expect(ackRow?.classList.contains("message-row--assistant")).toBe(true);
  });

  it("still opens a second divider when the transcript reaches the output step", () => {
    // Non-vacuous counterpart: the single divider above must be a consequence
    // of the pair sharing a step, not of dividers having stopped working.
    const { container } = render(
      <GuidedChatHistory
        chatHistory={[
          ...SEEDED_GOAL,
          { ...TURN_USER, seq: 2, step: "step_2_sink", content: "what about outputs?" },
        ]}
      />,
    );

    const dividers = container.querySelectorAll(".bubble-system--stage");
    expect(dividers).toHaveLength(2);
    expect(dividers[1].textContent).toBe("Output stage");
  });
});
