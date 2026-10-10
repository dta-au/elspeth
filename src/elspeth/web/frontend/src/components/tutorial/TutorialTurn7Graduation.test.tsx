import { beforeEach, describe, expect, it, vi } from "vitest";
import { act, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { resetStore } from "@/test/store-helpers";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { useSessionStore } from "@/stores/sessionStore";
import * as api from "@/api/client";
import { TutorialTurn7Graduation } from "./TutorialTurn7Graduation";

vi.mock("@/api/client", () => ({
  createSession: vi.fn(),
  fetchUserComposerPreferences: vi.fn(),
  updateUserComposerPreferences: vi.fn(),
}));

describe("TutorialTurn7Graduation", () => {
  beforeEach(() => {
    resetStore(usePreferencesStore);
    resetStore(useSessionStore);
    vi.clearAllMocks();
    usePreferencesStore.setState({
      loaded: true,
      tutorialCompletedAt: null,
      tutorialCompleted: false,
    });
    // Spyable store actions so graduation's rename + land-on-pipeline path can
    // be asserted without exercising the real implementations (which hit
    // unmocked api routes). selectSession lands the user on the built pipeline;
    // the stub mirrors the real action's contract by setting activeSessionId.
    useSessionStore.setState({
      renameSession: vi.fn().mockResolvedValue(undefined),
      loadSessions: vi.fn().mockResolvedValue(undefined),
      selectSession: vi.fn().mockImplementation(async (id: string) => {
        useSessionStore.setState({ activeSessionId: id, compositionStateLoaded: true });
      }),
    } as never);
    vi.mocked(api.createSession).mockResolvedValue({
      id: "session-empty",
      title: "New session",
      created_at: "2026-05-19T12:30:00Z",
      updated_at: "2026-05-19T12:30:00Z",
    });
    // Completion is saved in one request.
    vi.mocked(api.updateUserComposerPreferences).mockImplementation(
      async (body) => ({
        freeform_intro_dismissed_at: null,
        tutorial_completed_at:
          body.tutorial_completed_at === undefined
            ? null
            : body.tutorial_completed_at,
        tutorial_stage: null,
        tutorial_session_id: null,
        tutorial_run_id: null,
        tutorial_source_data_hash: null,
        show_advanced: false,
        updated_at: "2026-05-19T12:35:00Z",
      }),
    );
  });

  it("does not create a skipped session or publish completion after the save loses account ownership", async () => {
    let resolve!: (payload: Awaited<ReturnType<typeof api.updateUserComposerPreferences>>) => void;
    vi.mocked(api.updateUserComposerPreferences).mockReturnValueOnce(new Promise((yes) => { resolve = yes; }));
    render(<TutorialTurn7Graduation sessionId={null} skipped={true} cancelled={false} />);
    await userEvent.click(screen.getByRole("button", { name: "Take me to the composer" }));
    expect(api.updateUserComposerPreferences).toHaveBeenCalledTimes(1);
    act(() => usePreferencesStore.getState().bindPrincipal("new-user", "local"));
    await act(async () => {
      resolve({ freeform_intro_dismissed_at: null, tutorial_completed_at: "old completion", tutorial_stage: null, tutorial_session_id: null, tutorial_run_id: null, tutorial_source_data_hash: null, show_advanced: false, updated_at: null });
    });
    expect(api.createSession).not.toHaveBeenCalled();
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();
  });

  it("does not publish a non-skipped departure after unmount during its PATCH", async () => {
    let resolve!: (payload: Awaited<ReturnType<typeof api.updateUserComposerPreferences>>) => void;
    vi.mocked(api.updateUserComposerPreferences).mockReturnValueOnce(new Promise((yes) => { resolve = yes; }));
    const view = render(<TutorialTurn7Graduation sessionId="tutorial" skipped={false} cancelled={false} />);
    await userEvent.click(screen.getByRole("button", { name: "Take me to the composer" }));
    await waitFor(() => expect(api.updateUserComposerPreferences).toHaveBeenCalledTimes(1));
    view.unmount();
    await act(async () => {
      resolve({ freeform_intro_dismissed_at: null, tutorial_completed_at: "persisted completion", tutorial_stage: null, tutorial_session_id: null, tutorial_run_id: null, tutorial_source_data_hash: null, show_advanced: false, updated_at: null });
    });
    expect(usePreferencesStore.getState().tutorialCompletedAt).toBe("persisted completion");
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
  });

  it("focuses the heading, emits the graduation event, and renders the learning bullets", async () => {
    const eventListener = vi.fn();
    window.addEventListener("tutorial_graduation_shown", eventListener);

    render(
      <TutorialTurn7Graduation
        sessionId="sess-new"
        skipped={false}
        cancelled={false}
        onBack={() => undefined}
      />,
    );

    const heading = screen.getByRole("heading", {
      name: "You're ready to use the composer.",
    });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(eventListener).toHaveBeenCalledTimes(1);
    expect(screen.getByText("What you built is AI-generated.")).toBeInTheDocument();
    expect(screen.getByText("Read before you run.")).toBeInTheDocument();
    expect(screen.getByText("Ask ELSPETH.")).toBeInTheDocument();
    expect(screen.getByText("LLMs are confident even when they're wrong.")).toBeInTheDocument();
    expect(screen.getByText(/ask in the chat panel/i)).toBeInTheDocument();

    window.removeEventListener("tutorial_graduation_shown", eventListener);
  });

  it("marks the tutorial graduated and lands the user on the built pipeline", async () => {
    const user = userEvent.setup();
    render(
      <TutorialTurn7Graduation
        sessionId="sess-new"
        skipped={false}
        cancelled={false}
        onBack={() => undefined}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "Take me to the composer" }),
    );

    await waitFor(() => {
      expect(api.updateUserComposerPreferences).toHaveBeenCalledWith({
        tutorial_completed_at: expect.any(String),
        tutorial_completed_via: "complete",
      });
    });
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(true);
    // Land on the pipeline the user built, not a fresh empty session: the list
    // is refreshed (so the tutorial session appears in the switcher) and the
    // tutorial session becomes active.
    expect(useSessionStore.getState().loadSessions).toHaveBeenCalledTimes(1);
    expect(useSessionStore.getState().selectSession).toHaveBeenCalledWith("sess-new");
    expect(useSessionStore.getState().activeSessionId).toBe("sess-new");
    expect(api.createSession).not.toHaveBeenCalled();
  });

  it("renames the tutorial session and saves explicit completion intent", async () => {
    const user = userEvent.setup();
    render(
      <TutorialTurn7Graduation
        sessionId="sess-new"
        skipped={false}
        cancelled={false}
        onBack={() => undefined}
      />,
    );
    await user.click(
      screen.getByRole("button", { name: "Take me to the composer" }),
    );
    await waitFor(() =>
      expect(useSessionStore.getState().renameSession).toHaveBeenCalledWith(
        "sess-new",
        "First-run tutorial",
      ),
    );
    expect(api.updateUserComposerPreferences).toHaveBeenCalledWith({
      tutorial_completed_at: expect.any(String),
      tutorial_completed_via: "complete",
    });
  });

  it("does not rename a skipped tutorial session and saves completion", async () => {
    const user = userEvent.setup();
    render(
      <TutorialTurn7Graduation
        sessionId={null}
        skipped={true}
        cancelled={false}
        onBack={() => undefined}
      />,
    );
    await user.click(
      screen.getByRole("button", { name: "Take me to the composer" }),
    );
    await waitFor(() => {
      expect(api.updateUserComposerPreferences).toHaveBeenCalledWith({
        tutorial_completed_at: expect.any(String),
        tutorial_completed_via: "skip",
      });
    });
    expect(useSessionStore.getState().renameSession).not.toHaveBeenCalled();
  });

  it("renders the cancellation acknowledgement when the run was cancelled", () => {
    render(
      <TutorialTurn7Graduation
        sessionId="sess-new"
        skipped={false}
        cancelled={true}
        onBack={undefined}
      />,
    );
    expect(
      screen.getByText(/Your run was cancelled/i),
    ).toBeInTheDocument();
  });

  it("omits the Back button when no prior step is available", () => {
    render(
      <TutorialTurn7Graduation
        sessionId={null}
        skipped={true}
        cancelled={false}
        onBack={undefined}
      />,
    );
    expect(screen.queryByRole("button", { name: "Back" })).toBeNull();
  });

  it("shows a role alert on completion failure and keeps Back usable", async () => {
    const user = userEvent.setup();
    const onBack = vi.fn();
    vi.mocked(api.updateUserComposerPreferences).mockRejectedValueOnce(
      new Error("network down"),
    );
    render(
      <TutorialTurn7Graduation
        sessionId="sess-new"
        skipped={false}
        cancelled={false}
        onBack={onBack}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "Take me to the composer" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent("network down");
    expect(api.createSession).not.toHaveBeenCalled();

    await user.click(screen.getByRole("button", { name: "Back" }));
    expect(onBack).toHaveBeenCalledTimes(1);
  });

  it("does not save graduation when the composer cannot open the built pipeline", async () => {
    const user = userEvent.setup();
    // selectSession resolves but leaves the session inactive (e.g. a 404 on
    // load cleared activeSessionId); graduation must surface the failure and
    // must NOT publish completion.
    useSessionStore.setState({
      selectSession: vi.fn().mockImplementation(async () => {
        useSessionStore.setState({ activeSessionId: null });
      }),
    } as never);
    render(
      <TutorialTurn7Graduation
        sessionId="sess-new"
        skipped={false}
        cancelled={false}
        onBack={() => undefined}
      />,
    );

    await user.click(
      screen.getByRole("button", { name: "Take me to the composer" }),
    );

    expect(await screen.findByRole("alert")).toHaveTextContent(
      "The composer could not open your pipeline.",
    );
    expect(api.updateUserComposerPreferences).not.toHaveBeenCalled();
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
    expect(useSessionStore.getState().activeSessionId).toBeNull();
  });

  it("refuses graduation when the session ID loaded but its state did not", async () => {
    useSessionStore.setState({ selectSession: vi.fn().mockImplementation(async (id: string) => {
      useSessionStore.setState({ activeSessionId: id, compositionStateLoaded: false });
    }) });
    const user = userEvent.setup();
    render(<TutorialTurn7Graduation sessionId="sess-new" skipped={false} cancelled={false} />);
    await user.click(screen.getByRole("button", { name: "Take me to the composer" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("The tutorial session has not loaded");
    expect(api.updateUserComposerPreferences).not.toHaveBeenCalled();
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
  });

  it("keeps a pending compose visible and allows retry without publishing completion", async () => {
    useSessionStore.setState({ isComposing: true });
    const user = userEvent.setup();
    render(<TutorialTurn7Graduation sessionId="sess-new" skipped={false} cancelled={false} />);
    await user.click(screen.getByRole("button", { name: "Take me to the composer" }));
    expect(await screen.findByRole("alert")).toHaveTextContent("current Composer operation");
    expect(api.updateUserComposerPreferences).not.toHaveBeenCalled();
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
    expect(screen.getByRole("button", { name: "Take me to the composer" })).toBeEnabled();
    useSessionStore.setState({ isComposing: false });
    await user.click(screen.getByRole("button", { name: "Take me to the composer" }));
    await waitFor(() => expect(usePreferencesStore.getState().tutorialCompleted).toBe(true));
    expect(useSessionStore.getState().activeSessionId).toBe("sess-new");
    expect(api.updateUserComposerPreferences).toHaveBeenCalledTimes(1);
  });
});

describe("TutorialTurn7Graduation — skip-variant copy (elspeth-918f4434b3)", () => {
  it("renders honest future-tense bullets for the skipped path", () => {
    render(
      <TutorialTurn7Graduation
        sessionId={null}
        skipped={true}
        cancelled={false}
      />,
    );

    // The just-ran/just-practised claims are false on the skip path and
    // must not render.
    expect(screen.queryByText("What you built is AI-generated.")).toBeNull();
    expect(
      screen.queryByText(/the same gestures you just practised/i),
    ).toBeNull();
    // The skip variant carries the same lessons without the false claims.
    expect(
      screen.getByText("What the composer builds is AI-generated."),
    ).toBeInTheDocument();
    expect(screen.getByText("Read before you run.")).toBeInTheDocument();
    expect(
      screen.getByText(/nothing executes without your say-so/i),
    ).toBeInTheDocument();
    // Shared bullets (no just-ran claims) render on both paths.
    expect(screen.getByText("Ask ELSPETH.")).toBeInTheDocument();
    expect(
      screen.getByText(/same freeform conversation/i),
    ).toBeInTheDocument();
    expect(
      screen.getByText("LLMs are confident even when they're wrong."),
    ).toBeInTheDocument();
  });

  it("keeps the completed-path bullets for a real run", () => {
    render(
      <TutorialTurn7Graduation
        sessionId="sess-new"
        skipped={false}
        cancelled={false}
        onBack={() => undefined}
      />,
    );
    expect(
      screen.getByText("What you built is AI-generated."),
    ).toBeInTheDocument();
    expect(
      screen.queryByText("What the composer builds is AI-generated."),
    ).toBeNull();
  });

  // The copy must name a surface that EXISTS and that renders the record
  // (elspeth-4f69b267dd). Ruling D2 (2026-09-20): that surface is the Graph
  // tab's Approvals table — Checks shows only a count row.
  it("both variants point at the pipeline's Approvals tab — never Checks, a nonexistent 'Audit page' or the retired Audit drawer", () => {
    const { unmount } = render(
      <TutorialTurn7Graduation
        sessionId="sess-new"
        skipped={false}
        cancelled={false}
      />,
    );
    expect(
      screen.getByText(/on your pipeline's Approvals tab/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Checks tab/)).toBeNull();
    expect(screen.queryByText(/Audit page/)).toBeNull();
    expect(screen.queryByText(/Audit panel/)).toBeNull();
    unmount();

    render(
      <TutorialTurn7Graduation
        sessionId={null}
        skipped={true}
        cancelled={false}
      />,
    );
    expect(
      screen.getByText(/on each pipeline's Approvals tab/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/Checks tab/)).toBeNull();
    expect(screen.queryByText(/Audit page/)).toBeNull();
    expect(screen.queryByText(/Audit panel/)).toBeNull();
  });
});
