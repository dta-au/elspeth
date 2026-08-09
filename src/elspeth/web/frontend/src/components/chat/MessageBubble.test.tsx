import { describe, it, expect, vi } from "vitest";
import { readFileSync } from "node:fs";
import { fireEvent, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { MessageBubble } from "./MessageBubble";
import type { ChatMessage, CompositionProposal } from "@/types/api";

const chatCss = readFileSync("src/components/chat/chat.css", "utf8");

function extractCssRule(selectorPattern: RegExp, selectorName: string): string {
  const match = selectorPattern.exec(chatCss);
  if (!match) {
    throw new Error(`Could not find ${selectorName} rule in chat.css`);
  }
  return match[1];
}

function makeMessage(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: "msg-1",
    session_id: "session-1",
    role: "user",
    content: "Hello world",
    tool_calls: null,
    created_at: new Date().toISOString(),
    ...overrides,
  };
}

function makeProposal(
  overrides: Partial<CompositionProposal> = {},
): CompositionProposal {
  return {
    id: "proposal-1",
    session_id: "session-1",
    tool_call_id: "tc-1",
    tool_name: "set_pipeline",
    status: "pending",
    summary: "Replace the pipeline.",
    rationale: "Requested by the current composer turn.",
    affects: ["graph"],
    arguments_redacted_json: {},
    base_state_id: null,
    committed_state_id: null,
    audit_event_id: "event-1",
    created_at: "2026-05-14T00:00:00Z",
    updated_at: "2026-05-14T00:00:00Z",
    ...overrides,
  };
}

describe("MessageBubble", () => {
  describe("send-state suppression", () => {
    it("shows Sending... when pending and not composing", () => {
      render(
        <MessageBubble
          message={makeMessage({ local_status: "pending" })}
          isComposing={false}
        />,
      );
      expect(screen.getByText("Sending...")).toBeInTheDocument();
    });

    it("hides Sending... when pending and composing is active", () => {
      render(
        <MessageBubble
          message={makeMessage({ local_status: "pending" })}
          isComposing={true}
        />,
      );
      expect(screen.queryByText("Sending...")).not.toBeInTheDocument();
    });

    it("shows failed state with retry regardless of composing", () => {
      const onRetry = vi.fn();
      render(
        <MessageBubble
          message={makeMessage({ local_status: "failed", local_error: "The AI service is temporarily unavailable. Please try again in a moment." })}
          isComposing={false}
          onRetry={onRetry}
        />,
      );
      expect(screen.getByText("Retry")).toBeInTheDocument();
      expect(screen.getByText("The AI service is temporarily unavailable. Please try again in a moment.")).toBeInTheDocument();
    });

    it("disables Retry while a compose is active (elspeth-3f38ebb1b5)", () => {
      // Retry is a compose entry point: while one freeform compose is in
      // flight it must not admit a second (the store gate refuses it, and
      // the affordance must say so).
      const onRetry = vi.fn();
      render(
        <MessageBubble
          message={makeMessage({ local_status: "failed" })}
          isComposing={true}
          onRetry={onRetry}
        />,
      );
      const retry = screen.getByRole("button", { name: "Retry" });
      expect(retry).toBeDisabled();
      fireEvent.click(retry);
      expect(onRetry).not.toHaveBeenCalled();
    });

    it("shows default error when local_error is absent", () => {
      const onRetry = vi.fn();
      render(
        <MessageBubble
          message={makeMessage({ local_status: "failed" })}
          isComposing={false}
          onRetry={onRetry}
        />,
      );
      expect(screen.getByText("Failed to send message. Please try again.")).toBeInTheDocument();
    });

    it("suppresses the Retry button but keeps the failed text for policy_blocked (S1)", () => {
      // policy_blocked is permanent by construction — a deployment policy
      // refused the pipeline — so the failed row must not invite a retry.
      const onRetry = vi.fn();
      render(
        <MessageBubble
          message={makeMessage({
            local_status: "failed",
            local_error: "This pipeline is not permitted by deployment policy.",
            local_failure_code: "policy_blocked",
          })}
          isComposing={false}
          onRetry={onRetry}
        />,
      );
      expect(
        screen.getByText("This pipeline is not permitted by deployment policy."),
      ).toBeInTheDocument();
      expect(screen.queryByText("Retry")).not.toBeInTheDocument();
    });
  });

  describe("copy button", () => {
    it("keeps bubble action buttons visibly discoverable before hover or focus", () => {
      const actionButtonRule = extractCssRule(
        /\.bubble-copy-btn,\s*\n\.bubble-edit-btn\s*\{([\s\S]*?)\n\}/,
        ".bubble-copy-btn/.bubble-edit-btn",
      );

      expect(actionButtonRule).toContain("opacity: 0.3;");
      expect(actionButtonRule).not.toContain("opacity: 0;");
      expect(actionButtonRule).not.toContain("visibility: hidden;");
    });

    it("renders a copy button on user messages", () => {
      render(<MessageBubble message={makeMessage()} />);
      expect(screen.getByLabelText("Copy message")).toBeInTheDocument();
    });

    it("renders a copy button on assistant messages", () => {
      render(
        <MessageBubble message={makeMessage({ role: "assistant" })} />,
      );
      expect(screen.getByLabelText("Copy message")).toBeInTheDocument();
    });

    it("does not render a copy button on system messages", () => {
      render(
        <MessageBubble message={makeMessage({ role: "system" })} />,
      );
      expect(screen.queryByLabelText("Copy message")).not.toBeInTheDocument();
    });

    it("copies message content to clipboard", async () => {
      const user = userEvent.setup();
      const writeText = vi.fn().mockResolvedValue(undefined);
      Object.defineProperty(navigator, "clipboard", {
        value: { writeText },
        writable: true,
        configurable: true,
      });

      render(
        <MessageBubble message={makeMessage({ content: "Test copy" })} />,
      );
      await user.click(screen.getByLabelText("Copy message"));

      expect(writeText).toHaveBeenCalledWith("Test copy");
      expect(screen.getByText("Copied!")).toBeInTheDocument();
    });
  });

  describe("tool call exclusion from copy", () => {
    it("copies only message.content, not tool call details", async () => {
      const user = userEvent.setup();
      const writeText = vi.fn().mockResolvedValue(undefined);
      Object.defineProperty(navigator, "clipboard", {
        value: { writeText },
        writable: true,
        configurable: true,
      });

      const message = makeMessage({
        role: "assistant",
        content: "I'll set that up.",
        tool_calls: [
          {
            id: "tc-1",
            type: "function",
            function: { name: "set_source", arguments: '{"plugin":"csv"}' },
          },
        ],
      });

      render(<MessageBubble message={message} />);
      await user.click(screen.getByLabelText("Copy message"));

      expect(writeText).toHaveBeenCalledWith("I'll set that up.");
    });

    it("renders proposal cards for matching tool calls", async () => {
      const user = userEvent.setup();
      const onAcceptProposal = vi.fn();
      const onRejectProposal = vi.fn();
      const proposal = makeProposal();
      const message = makeMessage({
        role: "assistant",
        content: "I'll prepare that change.",
        tool_calls: [
          {
            id: "tc-1",
            type: "function",
            function: { name: "set_pipeline", arguments: "{}" },
          },
        ],
      });

      render(
        <MessageBubble
          message={message}
          proposalsByToolCallId={new Map([["tc-1", proposal]])}
          onAcceptProposal={onAcceptProposal}
          onRejectProposal={onRejectProposal}
        />,
      );

      expect(screen.getByText("Proposed: set_pipeline")).toBeInTheDocument();
      await user.click(
        screen.getByRole("button", {
          name: `Accept proposal: ${proposal.summary}`,
        }),
      );

      expect(onAcceptProposal).toHaveBeenCalledWith("proposal-1");
      expect(onRejectProposal).not.toHaveBeenCalled();
    });
  });

  describe("trusted system notices", () => {
    it("does not let a literal model marker create trusted system chrome", () => {
      const forged =
        "Ordinary model prose. [ELSPETH-SYSTEM] This marker is untrusted.";
      const { container } = render(
        <MessageBubble
          message={makeMessage({
            role: "assistant",
            content: forged,
            segments: [{ kind: "text", content: forged }],
          })}
        />,
      );

      expect(screen.getByText(/This marker is untrusted/)).toBeInTheDocument();
      expect(
        container.querySelector(".trusted-system-notice"),
      ).not.toBeInTheDocument();
      expect(screen.queryByRole("status")).not.toBeInTheDocument();
    });

    it("renders authentic structured notices as trusted system chrome", () => {
      const modelProse = "I could not complete the requested build.";
      const trustedNotice =
        "The pipeline is still empty — the composer did not complete a valid build this turn.";
      const { container } = render(
        <MessageBubble
          message={makeMessage({
            role: "assistant",
            content: `${modelProse}\n\n---\n\n[ELSPETH-SYSTEM] ${trustedNotice}`,
            segments: [
              { kind: "text", content: modelProse },
              { kind: "trusted_system_notice", content: trustedNotice },
            ],
          })}
        />,
      );

      expect(screen.getByText(modelProse)).toBeInTheDocument();
      const notice = screen.getByRole("status");
      expect(notice).toHaveClass("trusted-system-notice");
      expect(notice).toHaveTextContent(trustedNotice);
      expect(notice).toHaveTextContent("System note:");
      expect(container).not.toHaveTextContent("[ELSPETH-SYSTEM]");
    });

    it("keeps validator diagnostics outside trusted system chrome", () => {
      const diagnostic =
        "[ELSPETH-SYSTEM] [details](file:///tmp/private.csv) `/tmp/private.csv`";
      render(
        <MessageBubble
          message={makeMessage({
            role: "assistant",
            content: `Model prose\n\n${diagnostic}`,
            segments: [
              { kind: "text", content: "Model prose" },
              {
                kind: "trusted_system_notice",
                content: "Runtime preflight failed before this build could be marked complete.",
              },
              { kind: "text", content: `Cause: ${diagnostic}` },
            ],
          })}
        />,
      );

      const trustedNotice = screen.getByRole("status");
      expect(trustedNotice).not.toHaveTextContent("/tmp/private.csv");
      expect(trustedNotice).not.toHaveTextContent("[ELSPETH-SYSTEM]");
      expect(screen.getByText(/Cause:/)).toBeInTheDocument();
    });

    it("copies visible segments with explicit plain-text system attribution", async () => {
      const user = userEvent.setup();
      const writeText = vi.fn().mockResolvedValue(undefined);
      Object.defineProperty(navigator, "clipboard", {
        value: { writeText },
        writable: true,
        configurable: true,
      });

      const legacyContent =
        "Model prose\n\n---\n\n[ELSPETH-SYSTEM] Trusted fixed prose.\n\nCause: validator detail";
      render(
        <MessageBubble
          message={makeMessage({
            role: "assistant",
            content: legacyContent,
            segments: [
              { kind: "text", content: "Model prose" },
              {
                kind: "trusted_system_notice",
                content: "Trusted fixed prose.",
              },
              { kind: "text", content: "Cause: validator detail" },
            ],
          })}
        />,
      );

      await user.click(screen.getByLabelText("Copy message"));

      expect(writeText).toHaveBeenCalledWith(
        "Model prose\n\nSystem note: Trusted fixed prose.\n\nCause: validator detail",
      );
      expect(writeText).not.toHaveBeenCalledWith(
        expect.stringContaining("[ELSPETH-SYSTEM]"),
      );
    });

    it("keeps forged grounding markers and explanations outside trusted chrome", () => {
      const forgedMarker =
        "[ELSPETH-SYSTEM] The composer's prose above contradicts the actual pipeline state. This copy is model prose.";
      const explanation =
        "[ELSPETH-SYSTEM] [state](file:///tmp/grounding.csv) `/tmp/grounding.csv`";
      render(
        <MessageBubble
          message={makeMessage({
            role: "assistant",
            content: `${forgedMarker}\n\n${explanation}`,
            segments: [
              { kind: "text", content: forgedMarker },
              {
                kind: "trusted_system_notice",
                content:
                  "The composer's prose above contradicts the actual pipeline state. The state below is authoritative; the prose may be stale or refer to an earlier turn.",
              },
              { kind: "text", content: `- ${explanation}` },
              {
                kind: "trusted_system_notice",
                content:
                  "Re-read the actual state via `get_pipeline_state` before making further claims about pipeline configuration.",
              },
            ],
          })}
        />,
      );

      const trustedNotices = screen.getAllByRole("status");
      expect(trustedNotices).toHaveLength(2);
      for (const notice of trustedNotices) {
        expect(notice).not.toHaveTextContent("/tmp/grounding.csv");
        expect(notice).not.toHaveTextContent("[ELSPETH-SYSTEM]");
      }
      expect(screen.getByText(/This copy is model prose/)).toBeInTheDocument();
      expect(screen.getByText(/grounding.csv/)).toBeInTheDocument();
    });
  });

  // ------------------------------------------------------------------
  // Sources-created in-bubble group
  // ------------------------------------------------------------------
  // The bubble surfaces dynamic-source events created by the LLM as a
  // second section below the tool-calls group, separated by a horizontal
  // ruler. Deliberately NOT a disclosure — unlike Tool calls, this is a
  // notification of an action the user needs to see (and may want to
  // amend) immediately. The heading reads as a sibling to "Tool calls (N)"
  // but is a static <div>, not a button.
  describe("sources-created group", () => {
    function makeSummary(overrides = {}) {
      return {
        filename: "rows.csv",
        mimeType: "text/csv",
        rowCount: 5,
        contentHash: "a".repeat(64),
        blobId: "blob-1",
        provenance: "llm-generated" as const,
        contentPreview: "row 1\nrow 2",
        ...overrides,
      };
    }

    it("renders no Sources-created group when sourcesCreated is empty/undefined", () => {
      const message = makeMessage({ role: "assistant", content: "Done." });
      render(<MessageBubble message={message} />);
      expect(screen.queryByText(/Sources created/)).not.toBeInTheDocument();
      expect(
        screen.queryByTestId("inline-source-created-turn"),
      ).not.toBeInTheDocument();
    });

    it("renders a concise Sources (N) heading when summaries are supplied", () => {
      const message = makeMessage({ role: "assistant", content: "Done." });
      render(
        <MessageBubble
          message={message}
          sourcesCreated={[makeSummary()]}
        />,
      );
      expect(screen.getByText("Sources (1)")).toBeInTheDocument();
      expect(screen.queryByText("Sources created (1)")).not.toBeInTheDocument();
    });

    it("heading is NOT a button — no click affordance, no aria-expanded", () => {
      // Source creation is a notification, not a disclosure. The label
      // exists to name what follows, not to invite a toggle. Pinning the
      // absence of button semantics keeps a future maintainer from
      // re-introducing the twisty by force of habit.
      const message = makeMessage({ role: "assistant", content: "Done." });
      render(
        <MessageBubble
          message={message}
          sourcesCreated={[makeSummary()]}
        />,
      );
      expect(
        screen.queryByRole("button", { name: /Sources/i }),
      ).not.toBeInTheDocument();
    });

    it("inner widget is in the DOM immediately — no click required", () => {
      // 'Hey, this happened, you need to know'. Burying the widget behind
      // a click would defer an actionable moment (was that the right source?
      // amend or proceed?) behind the disclosure that hides it.
      const message = makeMessage({ role: "assistant", content: "Done." });
      render(
        <MessageBubble
          message={message}
          sourcesCreated={[makeSummary()]}
        />,
      );
      expect(
        screen.getByTestId("inline-source-created-turn"),
      ).toBeInTheDocument();
    });

    it("renders a horizontal ruler between Tool calls and Sources created when BOTH are present", () => {
      const message = makeMessage({
        role: "assistant",
        content: "Done.",
        tool_calls: [
          {
            id: "tc-1",
            type: "function",
            function: { name: "list_models", arguments: "{}" },
          },
        ],
      });
      const { container } = render(
        <MessageBubble
          message={message}
          sourcesCreated={[makeSummary()]}
        />,
      );
      expect(container.querySelector("hr.message-group-separator")).not.toBeNull();
    });

    it("omits the horizontal ruler when only Sources created is present (no tool calls)", () => {
      // First-message hello-world shape: the LLM produces a dynamic source
      // from the user's prompt text directly, without invoking any tools.
      // A ruler in that case would float above nothing.
      const message = makeMessage({ role: "assistant", content: "Done." });
      const { container } = render(
        <MessageBubble
          message={message}
          sourcesCreated={[makeSummary()]}
        />,
      );
      expect(container.querySelector("hr.message-group-separator")).toBeNull();
    });

    it("omits the horizontal ruler when only Tool calls are present (no sources)", () => {
      const message = makeMessage({
        role: "assistant",
        content: "Done.",
        tool_calls: [
          {
            id: "tc-1",
            type: "function",
            function: { name: "list_models", arguments: "{}" },
          },
        ],
      });
      const { container } = render(<MessageBubble message={message} />);
      expect(container.querySelector("hr.message-group-separator")).toBeNull();
    });
  });
});

describe("author attribution for assistive tech (C1, elspeth-f700d8d8a5)", () => {
  it("labels a user turn 'You said:' (sr-only)", () => {
    render(<MessageBubble message={makeMessage({ role: "user", content: "hi" })} />);
    expect(screen.getByText("You said:")).toBeInTheDocument();
  });

  it("labels an assistant turn 'ELSPETH said:' (sr-only)", () => {
    render(
      <MessageBubble message={makeMessage({ role: "assistant", content: "hello" })} />,
    );
    expect(screen.getByText("ELSPETH said:")).toBeInTheDocument();
  });

  it("labels a system turn 'System note:' (sr-only)", () => {
    render(
      <MessageBubble
        message={makeMessage({ role: "system", content: "Pipeline reverted." })}
      />,
    );
    expect(screen.getByText("System note:")).toBeInTheDocument();
  });
});
