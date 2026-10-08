import { act, render, screen, waitFor } from "@testing-library/react";
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

vi.mock("@/api/client");
// The real step header (Build's title, instruction and Continue) around the
// authoring content; the store-bound workspace panes stay out of scope.
vi.mock("./TutorialWorkspaceFrame", () => import("@/test/tutorialWorkspaceFrameStub"));

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
  });
  vi.mocked(api.listInterpretationEvents).mockResolvedValue([]);
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

  it("waits for authoritative session state before enabling the brief", async () => {
    let settleState!: (value: null) => void;
    vi.mocked(api.fetchMessages).mockResolvedValue([]);
    vi.mocked(api.fetchCompositionProposals).mockResolvedValue([]);
    vi.mocked(api.fetchComposerPreferences).mockResolvedValue({ session_id: "session-1", trust_mode: "auto_commit", density_default: "high", interpretation_review_disabled: false, updated_at: "2026-10-06T00:00:00Z" });
    vi.mocked(api.fetchCompositionState).mockImplementationOnce(() => new Promise((resolve) => { settleState = resolve; }));
    useSessionStore.setState({ compositionStateLoaded: false });
    render(<TutorialFreeformShell sessionId="session-1" onCompleted={vi.fn()} />);
    expect(screen.queryByRole("button", { name: "Send tutorial brief" })).toBeNull();
    expect(api.submitComposerOperation).not.toHaveBeenCalled();
    await waitFor(() => expect(api.fetchCompositionState).toHaveBeenCalled());
    await act(async () => { settleState(null); });
    await waitFor(() => expect(screen.getByRole("button", { name: "Send tutorial brief" })).toBeEnabled());
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
