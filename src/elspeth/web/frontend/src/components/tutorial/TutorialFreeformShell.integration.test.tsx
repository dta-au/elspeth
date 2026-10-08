import { act, render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/api/client";
import { ChatPanel } from "@/components/chat/ChatPanel";
import { COMPOSE_USER_CANCEL_ABORT_REASON } from "@/config/composer";
import { useSessionStore } from "@/stores/sessionStore";
import { useBlobStore } from "@/stores/blobStore";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { resetStore } from "@/test/store-helpers";
import type { ChatMessage } from "@/types/index";
import { TutorialFreeformShell } from "./TutorialFreeformShell";
import type { TutorialStepHeader } from "./TutorialWorkspaceFrame";

vi.mock("@/api/client");
// Renders the step header (Build's title, instruction and Continue); the
// workspace panes stay out of scope.
vi.mock("./TutorialWorkspaceFrame", () => ({
  TutorialWorkspaceFrame: ({ children, header }: { children: React.ReactNode; header?: TutorialStepHeader }) => (
    <section>
      {header !== undefined && (
        <header>
          <h2>{header.title}</h2>
          {header.instruction}
          {header.notice}
          {header.actions}
        </header>
      )}
      {children}
    </section>
  ),
}));

const userMessage: ChatMessage = {
  id: "message-1",
  session_id: "session-1",
  role: "user",
  content: "Build a pipeline",
  tool_calls: null,
  created_at: "2026-09-28T00:00:00Z",
};

beforeEach(() => {
  vi.resetAllMocks();
  resetStore(useSessionStore);
  resetStore(useBlobStore);
  resetStore(useInterpretationEventsStore);
  useSessionStore.setState({
    activeSessionId: "session-1",
    compositionStateLoaded: true,
    composeTimeoutReady: true,
  });
  vi.mocked(api.getTutorialSample).mockResolvedValue({
    sample_urls: ["https://example.gov.au/project-1.html"],
  });
});

describe("tutorial with the ordinary chat controls", () => {
  it("lets Stop abort the initial brief's request", async () => {
    const user = userEvent.setup();
    let signal: AbortSignal | undefined;
    let finish!: () => void;
    useSessionStore.setState({
      sendMessage: vi.fn(async (content, requestSignal) => {
        signal = requestSignal;
        useSessionStore.setState({ isComposing: true, messages: [{ ...userMessage, content }] });
        await new Promise<void>((resolve) => { finish = resolve; });
        useSessionStore.setState({ isComposing: false });
      }),
    });
    render(<TutorialFreeformShell sessionId="session-1" onCompleted={vi.fn()} />);
    await user.click(await screen.findByRole("button", { name: "Send tutorial brief" }));
    try {
      await user.click(await screen.findByRole("button", { name: "Stop composing" }));
      expect(signal?.aborted).toBe(true);
      expect(signal?.reason).toBe(COMPOSE_USER_CANCEL_ABORT_REASON);
    } finally {
      await act(async () => { finish(); });
    }
  });

  it("waits for the compose timeout configuration before enabling the brief", async () => {
    useSessionStore.setState({ composeTimeoutReady: false });
    render(<TutorialFreeformShell sessionId="session-1" onCompleted={vi.fn()} />);
    const send = await screen.findByRole("button", { name: "Send tutorial brief" });
    expect(send).toBeDisabled();
    act(() => useSessionStore.setState({ composeTimeoutReady: true }));
    expect(send).toBeEnabled();
  });

  it("keeps the tutorial bound to its session by hiding the fork action", async () => {
    useSessionStore.setState({ messages: [userMessage] });
    render(<TutorialFreeformShell sessionId="session-1" onCompleted={vi.fn()} />);
    await screen.findByRole("button", { name: "Continue to Run" });
    expect(screen.queryByRole("button", { name: "Edit and fork from this message" })).toBeNull();
    expect(useSessionStore.getState().activeSessionId).toBe("session-1");
  });

  it("still offers fork in the ordinary chat panel", () => {
    useSessionStore.setState({ messages: [userMessage] });
    render(<ChatPanel />);
    expect(screen.getByRole("button", { name: "Edit and fork from this message" })).toBeEnabled();
  });
});
