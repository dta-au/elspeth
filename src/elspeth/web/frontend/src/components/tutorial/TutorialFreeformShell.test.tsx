import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/api/client";
import { useInterpretationEventsStore } from "@/stores/interpretationEventsStore";
import { useSessionStore } from "@/stores/sessionStore";
import { resetStore } from "@/test/store-helpers";
import type { ChatMessage, CompositionProposal, CompositionState } from "@/types/index";
import type { InterpretationEvent } from "@/types/interpretation";
import { TutorialFreeformShell } from "./TutorialFreeformShell";

vi.mock("@/api/client", () => ({
  getTutorialSample: vi.fn().mockResolvedValue({
    sample_urls: ["https://example.gov.au/project-1.html", "https://example.gov.au/project-2.html", "https://example.gov.au/project-3.html"],
  }),
  getTutorialReadiness: vi.fn().mockResolvedValue({ state_id: "state-1" }),
}));
vi.mock("@/components/chat/ChatPanel", () => ({ ChatPanelContent: () => <div>Ordinary freeform chat</div> }));
vi.mock("./TutorialWorkspaceFrame", () => ({
  TutorialWorkspaceFrame: ({ children }: { children: React.ReactNode }) => <section>{children}</section>,
}));

const userMessage = {
  id: "message-1",
  session_id: "session-1",
  role: "user",
  content: "Please build my pipeline",
} as ChatMessage;

beforeEach(() => {
  vi.clearAllMocks();
  resetStore(useSessionStore);
  resetStore(useInterpretationEventsStore);
  useSessionStore.setState({
    compositionStateLoaded: true,
    selectSession: vi.fn(async (id: string) => {
      useSessionStore.setState({ activeSessionId: id, compositionStateLoaded: true });
    }),
    sendMessage: vi.fn().mockResolvedValue(undefined),
  });
});

describe("TutorialFreeformShell", () => {
  it("hydrates the session again after a transient selection failure", async () => {
    const user = userEvent.setup();
    const selectSession = vi.fn(async (id: string) => {
      useSessionStore.setState({ activeSessionId: id, compositionStateLoaded: true, error: null });
    }).mockImplementationOnce(async (id: string) => {
      useSessionStore.setState({
        activeSessionId: id,
        compositionStateLoaded: true,
        error: "Failed to load session. Please refresh the page.",
      });
    });
    useSessionStore.setState({ selectSession });
    render(<TutorialFreeformShell sessionId="session-1" onCompleted={vi.fn()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent("Failed to load session");
    expect(api.getTutorialSample).not.toHaveBeenCalled();
    await user.click(screen.getByRole("button", { name: "Retry loading example" }));

    await screen.findByRole("button", { name: "Send tutorial brief" });
    expect(selectSession).toHaveBeenCalledTimes(2);
    expect(screen.queryByRole("alert")).toBeNull();
  });

  it("submits one complete brief through the ordinary freeform send action", async () => {
    const user = userEvent.setup();
    const onCompleted = vi.fn();
    const { rerender } = render(<TutorialFreeformShell sessionId="session-1" onCompleted={onCompleted} />);

    await user.click(await screen.findByRole("button", { name: "Send tutorial brief" }));

    const send = useSessionStore.getState().sendMessage;
    expect(send).toHaveBeenCalledTimes(1);
    const brief = vi.mocked(send).mock.calls[0]?.[0];
    expect(brief).toContain("https://example.gov.au/project-1.html");
    expect(brief).toContain("https://example.gov.au/project-3.html");
    expect(brief).toContain("CSV source");
    expect(brief).toContain("JSON file");
    expect(brief).toContain("summary");
    expect(brief).toContain("configured default LLM profile");
    expect(screen.getByText("Ordinary freeform chat")).toBeInTheDocument();
    rerender(<TutorialFreeformShell sessionId="session-1" onCompleted={onCompleted} />);
    expect(send).toHaveBeenCalledTimes(1);
    expect(api.getTutorialReadiness).not.toHaveBeenCalled();
  });

  it("resumes an existing freeform transcript without resending the brief", async () => {
    useSessionStore.setState({ messages: [userMessage] });

    render(<TutorialFreeformShell sessionId="session-1" onCompleted={vi.fn()} />);

    await screen.findByText("Ordinary freeform chat");
    expect(screen.queryByRole("button", { name: "Send tutorial brief" })).toBeNull();
    expect(useSessionStore.getState().sendMessage).not.toHaveBeenCalled();
  });

  it("holds Build on a pending proposal and advances only for the committed state", async () => {
    const user = userEvent.setup();
    const onCompleted = vi.fn();
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionStateLoaded: true,
      compositionState: { id: "state-1" } as CompositionState,
      compositionProposals: [{ id: "proposal-1", status: "pending" } as CompositionProposal],
      messages: [userMessage],
    });
    render(<TutorialFreeformShell sessionId="session-1" onCompleted={onCompleted} />);

    const continueButton = await screen.findByRole("button", { name: "Continue to Run" });
    expect(continueButton).toBeDisabled();
    expect(api.getTutorialReadiness).not.toHaveBeenCalled();

    useSessionStore.setState({ compositionProposals: [] });
    await waitFor(() => expect(continueButton).toBeEnabled());
    await user.click(continueButton);
    await waitFor(() => expect(onCompleted).toHaveBeenCalledWith("session-1"));
    expect(api.getTutorialReadiness).toHaveBeenCalledWith("session-1");
  });

  it("does not advance while interpretation review is pending", async () => {
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionStateLoaded: true,
      compositionState: { id: "state-1" } as CompositionState,
      messages: [userMessage],
    });
    useInterpretationEventsStore.setState({
      pendingBySession: { "session-1": { "review-1": { id: "review-1" } as InterpretationEvent } },
    });
    render(<TutorialFreeformShell sessionId="session-1" onCompleted={vi.fn()} />);

    expect(await screen.findByRole("button", { name: "Continue to Run" })).toBeDisabled();
    expect(api.getTutorialReadiness).not.toHaveBeenCalled();
  });

  it("rejects a readiness answer for an old committed state", async () => {
    const user = userEvent.setup();
    const onCompleted = vi.fn();
    let answer!: (result: { state_id: string }) => void;
    vi.mocked(api.getTutorialReadiness).mockImplementationOnce(() => new Promise((resolve) => { answer = resolve; }));
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionStateLoaded: true,
      compositionState: { id: "state-1" } as CompositionState,
      messages: [userMessage],
    });
    render(<TutorialFreeformShell sessionId="session-1" onCompleted={onCompleted} />);
    await user.click(await screen.findByRole("button", { name: "Continue to Run" }));
    useSessionStore.setState({ compositionState: { id: "state-2" } as CompositionState });
    answer({ state_id: "state-1" });

    expect(await screen.findByRole("alert")).toHaveTextContent(/pipeline changed/i);
    expect(onCompleted).not.toHaveBeenCalled();
  });

  it("keeps the learner in Build after a backend not-ready response", async () => {
    const user = userEvent.setup();
    const onCompleted = vi.fn();
    vi.mocked(api.getTutorialReadiness).mockRejectedValueOnce({
      status: 409,
      detail: "The saved tutorial pipeline does not match the supported tutorial plugin set.",
    });
    useSessionStore.setState({
      activeSessionId: "session-1",
      compositionStateLoaded: true,
      compositionState: { id: "state-1" } as CompositionState,
      messages: [userMessage],
    });
    render(<TutorialFreeformShell sessionId="session-1" onCompleted={onCompleted} />);
    await user.click(await screen.findByRole("button", { name: "Continue to Run" }));

    expect(await screen.findByRole("alert")).toHaveTextContent(/does not match/i);
    expect(onCompleted).not.toHaveBeenCalled();
  });

  it("retries a transient sample-load failure without creating another session", async () => {
    const user = userEvent.setup();
    vi.mocked(api.getTutorialSample).mockRejectedValueOnce(new Error("Sample service temporarily unavailable"));
    render(<TutorialFreeformShell sessionId="session-1" onCompleted={vi.fn()} />);

    expect(await screen.findByRole("alert")).toHaveTextContent(/temporarily unavailable/i);
    await user.click(screen.getByRole("button", { name: "Retry loading example" }));

    expect(await screen.findByRole("button", { name: "Send tutorial brief" })).toBeInTheDocument();
    expect(api.getTutorialSample).toHaveBeenCalledTimes(2);
    expect(useSessionStore.getState().selectSession).toHaveBeenCalledTimes(1);
  });
});
