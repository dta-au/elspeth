import { StrictMode } from "react";
import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { HelloWorldTutorial } from "./HelloWorldTutorial";
import { TutorialTurn4Run } from "./TutorialTurn4Run";
import { usePreferencesStore } from "@/stores/preferencesStore";
import { useSessionStore } from "@/stores/sessionStore";
import { resetStore } from "@/test/store-helpers";

beforeEach(() => {
  resetStore(useSessionStore);
});

vi.mock("@/api/client", () => ({
  fetchPluginPolicy: vi.fn().mockResolvedValue({ data: { selections: [], control_modes: [] }, snapshotFingerprint: "fp" }),
  deleteTutorialOrphans: vi.fn().mockResolvedValue({ deleted_count: 0 }),
  // Mount-time resume validation lists live sessions; default includes the
  // canonical resume session so the existing resume tests read as "alive".
  fetchSessions: vi.fn().mockResolvedValue([
    {
      id: "sess-resume",
      title: "First-run tutorial (in progress)",
      created_at: "2026-05-19T12:00:00Z",
      updated_at: "2026-05-19T12:00:00Z",
    },
  ]),
  createSession: vi.fn().mockResolvedValue({
    id: "sess-new",
    title: "New session",
    created_at: "2026-05-19T12:00:00Z",
    updated_at: "2026-05-19T12:00:00Z",
  }),
  renameSession: vi.fn().mockResolvedValue({
    id: "sess-new",
    title: "First-run tutorial (in progress)",
    created_at: "2026-05-19T12:00:00Z",
    updated_at: "2026-05-19T12:00:00Z",
  }),
  // TutorialTurn4Run reaches the real api.runTutorialPipeline in the relocated
  // StrictMode dedup test below, so it must be mocked even though the staged
  // flow tests stop at the mocked freeform Build shell.
  runTutorialPipeline: vi.fn().mockResolvedValue({
    run_id: "run-1",
    output: {
      source_data_hash: "a7f3e2fullhash",
      rows: [{ url: "dta.gov.au", score: 9, rationale: "bold" }],
      discarded_row_count: 0,
    },
  }),
  sendTutorialAbandonBeacon: vi.fn(),
  // The chrome "Exit tutorial" control abandons an in-flight run via
  // TutorialTurn4Run's abandonTutorialRun, which fires this.
  cancelTutorialRun: vi.fn().mockResolvedValue({ cancelled: true }),
  // The tutorial-persistence slice (elspeth-918f4434b3): the component
  // persists stage transitions through preferencesStore, which calls this.
  // Body-aware echo mirroring the backend upsert (supplied fields land in
  // the response; completion clears progress server-side).
  updateUserComposerPreferences: vi.fn(async (body: Record<string, unknown>) => ({
    freeform_intro_dismissed_at: null,
    show_advanced: false,
    tutorial_completed_at: body.tutorial_completed_at ?? null,
    tutorial_stage: body.tutorial_completed_at ? null : (body.tutorial_stage ?? null),
    tutorial_session_id: body.tutorial_completed_at
      ? null
      : (body.tutorial_session_id ?? null),
    tutorial_run_id: body.tutorial_completed_at ? null : (body.tutorial_run_id ?? null),
    tutorial_source_data_hash: body.tutorial_completed_at
      ? null
      : (body.tutorial_source_data_hash ?? null),
    updated_at: "2026-07-02T00:00:00Z",
  })),
  fetchUserComposerPreferences: vi.fn(),
  // The resumed-audit test mounts the real TutorialTurn5AuditStory.
  getRunAuditSummary: vi.fn().mockResolvedValue({
    run_id: "run-resume",
    session_id: "sess-resume",
    llm_call_count: 3,
    source_data_hash: "hash-resume",
    started_at: "2026-07-02T00:00:00Z",
    plugin_versions: {},
    seeded_from_cache: false,
    cache_key: null,
  }),
}));

// Replace the embedded freeform surface with a one-button stub that fires the
// completion callback; TutorialFreeformShell.test.tsx covers real Build behavior.
// When set, the stub fires onSessionMissing TWICE on mount with its session
// id — simulating the live race where the shell's sample-load 404 handler
// and the mount-time membership check both detect the same dead resume.
let stubShellReportsSessionMissing = false;
// Per-test session id the stub hands to onCompleted. The run turn's
// StrictMode dedupe cache is module-level and keyed by sessionId, so tests
// that exercise the run step with a bespoke runTutorialPipeline mock must use
// a distinct id or they replay a previous test's cached run.
let stubBuildSessionId = "sess-new";

vi.mock("./TutorialFreeformShell", async () => {
  const { useEffect } = await import("react");
  return {
    TutorialFreeformShell: ({ sessionId, onCompleted, onSessionMissing }: {
      sessionId: string;
      onCompleted: (id: string) => void;
      onSessionMissing?: (id: string) => void;
    }) => {
      useEffect(() => {
        useSessionStore.setState({ activeSessionId: sessionId, compositionStateLoaded: true });
        if (stubShellReportsSessionMissing) {
          onSessionMissing?.(sessionId);
          onSessionMissing?.(sessionId);
        }
        // The test stub mirrors the real shell's mount-time session binding.
        // eslint-disable-next-line react-hooks/exhaustive-deps
      }, []);
      return (
        <button type="button" onClick={() => {
          useSessionStore.setState({
            activeSessionId: stubBuildSessionId,
            compositionStateLoaded: true,
          });
          onCompleted(stubBuildSessionId);
        }}>finish-build</button>
      );
    },
  };
});

// The run stage keeps the workspace frame (pipeline pane with the committed
// graph) mounted around the run's body. The frame's real panes are covered by
// the a11y suite and tests/e2e/tutorial.spec.ts; here the stand-in renders the
// real step header (where Run, Back and Continue live) around the authoring
// content, so the run step's own behaviour is what these tests exercise.
vi.mock("./TutorialWorkspaceFrame", () => import("@/test/tutorialWorkspaceFrameStub"));

/** The learner's explicit Run gesture on the run turn (I-1). */
async function clickRun(user: ReturnType<typeof userEvent.setup>): Promise<void> {
  await user.click(await screen.findByRole("button", { name: "Run" }));
}

describe("HelloWorldTutorial staged flow", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetStore(usePreferencesStore);
    stubShellReportsSessionMissing = false;
    stubBuildSessionId = "sess-new";
  });

  it("enters freeform Build after Welcome", async () => {
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    expect(await screen.findByRole("button", { name: "finish-build" })).toBeInTheDocument();
  });

  it("the run step waits for an explicit Run click — mounting it runs nothing", async () => {
    const api = await import("@/api/client");
    stubBuildSessionId = "sess-explicit-run";
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    await user.click(
      await screen.findByRole("button", { name: "finish-build" }),
    );

    // The run card sits inside the workspace frame so the pipeline pane
    // still shows the graph the learner just confirmed.
    const frame = await screen.findByTestId("tutorial-workspace-frame");
    expect(frame).toHaveAccessibleName(/tutorial run/i);
    expect(
      screen.getByRole("heading", { name: /ready to run/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run" })).toBeInTheDocument();
    expect(api.runTutorialPipeline).not.toHaveBeenCalled();
    // The store was bound by the Build shell; the run stage must not
    // re-hydrate a session that is already loaded.

    await clickRun(user);
    expect(await screen.findByText("bold")).toBeInTheDocument();
    expect(api.runTutorialPipeline).toHaveBeenCalledTimes(1);
  });

  it.each(["create", "rename"] as const)("Skip wins over a pending %s response", async (pendingStep) => {
    const api = await import("@/api/client");
    const user = userEvent.setup();
    let settle: (value: Awaited<ReturnType<typeof api.createSession>>) => void = () => undefined;
    const pending = new Promise<Awaited<ReturnType<typeof api.createSession>>>((resolve) => { settle = resolve; });
    vi.mocked(pendingStep === "create" ? api.createSession : api.renameSession).mockReturnValueOnce(pending);
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    await waitFor(() => expect(pendingStep === "create" ? api.createSession : api.renameSession).toHaveBeenCalled());
    await user.click(screen.getByRole("button", { name: "Skip the tutorial" }));
    await screen.findByRole("heading", { name: "You're ready to use the composer." });
    await act(async () => settle({
      id: "sess-new", title: "First-run tutorial (in progress)",
      created_at: "2026-05-19T12:00:00Z", updated_at: "2026-05-19T12:00:00Z",
    }));
    expect(screen.getByRole("heading", { name: "You're ready to use the composer." })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "finish-build" })).toBeNull();
    expect(useSessionStore.getState().activeSessionId).toBeNull();
    expect(api.updateUserComposerPreferences).not.toHaveBeenCalledWith(expect.objectContaining({ tutorial_stage: "build" }));
    expect(api.updateUserComposerPreferences).toHaveBeenCalledWith(expect.objectContaining({ tutorial_completed_via: "skip" }));
  });

  it("renders the welcome bookend first", () => {
    render(<HelloWorldTutorial />);
    expect(
      screen.getByRole("heading", { name: /welcome/i }),
    ).toBeInTheDocument();
  });

  it("labels the tutorial progress row visibly so it reads as a different hierarchy from the build stepper", async () => {
    // The visible
    // "Tutorial · <stage>" label is aria-hidden — the existing sr-only "Step N
    // of M" line stays the AT signal (the ARIA was already right).
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    const label = screen.getByText("Tutorial · Welcome — step 1 of 5");
    expect(label).toHaveAttribute("aria-hidden", "true");

    await user.click(screen.getByRole("button", { name: "Let's go" }));
    expect(
      await screen.findByText("Tutorial · Build — step 2 of 5"),
    ).toBeInTheDocument();
  });

  it("runs orphan cleanup on mount", async () => {
    const api = await import("@/api/client");
    render(<HelloWorldTutorial />);
    expect(api.deleteTutorialOrphans).toHaveBeenCalledTimes(1);
  });

  it("advances welcome -> freeform Build -> run on Build completion", async () => {
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "finish-build" }),
      ).toBeInTheDocument(),
    );
    await user.click(screen.getByRole("button", { name: "finish-build" }));
    await waitFor(() =>
      expect(
        screen.queryByRole("button", { name: "finish-build" }),
      ).toBeNull(),
    );
  });

  it("allows Back to freeform Build before Run but not after a result", async () => {
    // Distinct id: this test clicks Run, which populates the module-level
    // run cache for its session id.
    stubBuildSessionId = "sess-no-back";
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    await user.click(
      await screen.findByRole("button", { name: "finish-build" }),
    );
    // A pre-run pipeline can still be amended in freeform Build.
    await screen.findByRole("button", { name: "Run" });
    expect(screen.getByRole("button", { name: /^Back/ })).toBeInTheDocument();
    // After the Run click the run turn fetches via the mocked
    // runTutorialPipeline and renders its result row ("bold" rationale) plus
    // the primary "continue" button.
    await clickRun(user);
    expect(await screen.findByText("bold")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /^Back/ })).toBeNull();
  });

  it("tags the created tutorial session before entering Build", async () => {
    const api = await import("@/api/client");
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    await waitFor(() =>
      expect(api.renameSession).toHaveBeenCalledWith(
        "sess-new",
        "First-run tutorial (in progress)",
      ),
    );
    const createOrder = vi.mocked(api.createSession).mock
      .invocationCallOrder[0];
    const renameOrder = vi.mocked(api.renameSession).mock
      .invocationCallOrder[0];
    expect(createOrder).toBeLessThan(renameOrder);
  });

  it("surfaces createSession failure instead of stalling on welcome", async () => {
    const api = await import("@/api/client");
    vi.mocked(api.createSession).mockRejectedValueOnce(
      new Error("session service down"),
    );
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "session service down",
    );
    // Still on welcome — the Build shell never mounted.
    expect(
      screen.getByRole("heading", { name: /welcome/i }),
    ).toBeInTheDocument();
  });

  it("preflights composer availability before creating a tutorial session", async () => {
    const api = await import("@/api/client");
    render(
      <HelloWorldTutorial
        composerAvailable={false}
        composerUnavailableReason="Composer model openrouter/openai/gpt-4o is unavailable: missing OPENROUTER_API_KEY."
      />,
    );

    const start = screen.getByRole("button", { name: "Let's go" });
    expect(start).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "missing OPENROUTER_API_KEY",
    );
    expect(
      screen.getByRole("button", { name: "Skip the tutorial" }),
    ).toBeEnabled();
    expect(api.createSession).not.toHaveBeenCalled();
  });

  it("keeps ordinary authoring reachable while a missing tutorial profile disables only tutorial start", async () => {
    const api = await import("@/api/client");
    render(
      <HelloWorldTutorial
        composerAvailable={true}
        tutorialReady={false}
        tutorialUnavailableReason="Tutorial LLM profile is not configured"
      />,
    );

    expect(screen.getByRole("button", { name: "Let's go" })).toBeDisabled();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Tutorial LLM profile is not configured",
    );
    expect(screen.getByRole("button", { name: "Skip the tutorial" })).toBeEnabled();
    expect(api.createSession).not.toHaveBeenCalled();
  });

  // Relocated from the old big-bang test: TutorialTurn4Run dedups the run
  // request under StrictMode's double-invoke. This is the only coverage of
  // that behaviour, so it rides along with HelloWorldTutorial's suite rather
  // than being dropped with the removed turns.
  it("settles the run turn under React StrictMode without duplicating the run request", async () => {
    const api = await import("@/api/client");
    render(
      <StrictMode>
        <TutorialTurn4Run
          sessionId="strict-session"
          onCompleted={() => undefined}
          onCancelled={() => undefined}
          onBack={() => undefined}
        />
      </StrictMode>,
    );

    // Nothing runs on mount (I-1); the request leaves on the Run click.
    expect(api.runTutorialPipeline).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: "Run" }));
    expect(await screen.findByText("bold")).toBeInTheDocument();
    expect(api.runTutorialPipeline).toHaveBeenCalledTimes(1);
    const [body, signal] = vi.mocked(api.runTutorialPipeline).mock.calls[0];
    expect(body).toEqual({
      session_id: "strict-session",
    });
    expect(signal).toBeInstanceOf(AbortSignal);
  });
});

describe("HelloWorldTutorial — abandon beacon (F4)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetStore(usePreferencesStore);
    stubBuildSessionId = "sess-new";
  });

  it("does not fire the abandon beacon on pagehide while still on the welcome bookend", async () => {
    const api = await import("@/api/client");
    render(<HelloWorldTutorial />);
    // Nothing has started yet — there is no in-progress tutorial to abandon.
    window.dispatchEvent(new Event("pagehide"));
    expect(api.sendTutorialAbandonBeacon).not.toHaveBeenCalled();
  });

  it("fires the abandon beacon on pagehide once the tutorial is in progress", async () => {
    const api = await import("@/api/client");
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "finish-build" }),
      ).toBeInTheDocument(),
    );

    window.dispatchEvent(new Event("pagehide"));

    expect(api.sendTutorialAbandonBeacon).toHaveBeenCalledTimes(1);
  });

  it("does not fire the abandon beacon on pagehide once graduation is reached (skip path)", async () => {
    const api = await import("@/api/client");
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Skip the tutorial" }));
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "You're ready to use the composer." }),
      ).toBeInTheDocument(),
    );

    window.dispatchEvent(new Event("pagehide"));

    // Skip lands on the same terminal `graduation` step every completing path
    // reaches — the learner saw the tutorial through, so this is not an
    // abandon, regardless of which door they left through.
    expect(api.sendTutorialAbandonBeacon).not.toHaveBeenCalled();
  });

  it("removes its pagehide listener on unmount so a later pagehide cannot fire the beacon", async () => {
    const api = await import("@/api/client");
    const user = userEvent.setup();
    const { unmount } = render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    await waitFor(() =>
      expect(
        screen.getByRole("button", { name: "finish-build" }),
      ).toBeInTheDocument(),
    );

    unmount();
    window.dispatchEvent(new Event("pagehide"));

    expect(api.sendTutorialAbandonBeacon).not.toHaveBeenCalled();
  });
});

describe("HelloWorldTutorial — exit to freeform (elspeth-61591e64bb)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetStore(usePreferencesStore);
    stubBuildSessionId = "sess-new";
  });

  it("does not publish a pending exit after the tutorial unmounts", async () => {
    const api = await import("@/api/client");
    const user = userEvent.setup();
    const { unmount } = render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    const exit = await screen.findByRole("button", { name: "Exit tutorial" });
    await waitFor(() => expect(usePreferencesStore.getState().writing).toBe(false));
    let settle!: (value: Awaited<ReturnType<typeof api.updateUserComposerPreferences>>) => void;
    vi.mocked(api.updateUserComposerPreferences).mockReturnValueOnce(
      new Promise((resolve) => { settle = resolve; }),
    );

    await user.click(exit);
    await waitFor(() => expect(api.updateUserComposerPreferences).toHaveBeenCalledWith(
      expect.objectContaining({ tutorial_completed_via: "exit" }),
    ));
    unmount();
    const stamp = "2026-07-02T00:00:00Z";
    await act(async () => settle({
      freeform_intro_dismissed_at: null,
      show_advanced: false,
      tutorial_completed_at: stamp,
      tutorial_stage: null,
      tutorial_session_id: null,
      tutorial_run_id: null,
      tutorial_source_data_hash: null,
      updated_at: stamp,
    }));

    expect(usePreferencesStore.getState().tutorialCompletedAt).toBe(stamp);
    expect(usePreferencesStore.getState().tutorialCompleted).toBe(false);
  });

  it("persists the exit opt-out from freeform Build", async () => {
    const api = await import("@/api/client");
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    await user.click(await screen.findByRole("button", { name: "Exit tutorial" }));
    await waitFor(() =>
      expect(api.updateUserComposerPreferences).toHaveBeenCalledWith({
        tutorial_completed_at: expect.any(String),
        tutorial_completed_via: "exit",
      }),
    );
    // Unlike skip (which keeps the graduation farewell mounted), exit
    // publishes locally so App's showTutorial gate unmounts the shell and
    // the learner lands in freeform on the same session.
    await waitFor(() =>
      expect(usePreferencesStore.getState().tutorialCompleted).toBe(true),
    );
  });

  it("renders an Exit tutorial control on freeform Build", async () => {
    const api = await import("@/api/client");
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    await user.click(
      await screen.findByRole("button", { name: "Exit tutorial" }),
    );
    await waitFor(() =>
      expect(api.updateUserComposerPreferences).toHaveBeenCalledWith(
        expect.objectContaining({
          tutorial_completed_at: expect.any(String),
          tutorial_completed_via: "exit",
        }),
      ),
    );
    await waitFor(() =>
      expect(usePreferencesStore.getState().tutorialCompleted).toBe(true),
    );
  });

  it("renders the Exit tutorial control on the run step", async () => {
    // Distinct id: this test clicks Run (module-level run cache, see stub note).
    stubBuildSessionId = "sess-exit-control-run";
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    await user.click(
      await screen.findByRole("button", { name: "finish-build" }),
    );
    // Present on the pre-run card and after the run.
    await screen.findByRole("button", { name: "Run" });
    expect(
      screen.getByRole("button", { name: "Exit tutorial" }),
    ).toBeInTheDocument();
    await clickRun(user);
    expect(await screen.findByText("bold")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Exit tutorial" }),
    ).toBeInTheDocument();
  });

  it("renders the Exit tutorial control on the resumed audit step", async () => {
    usePreferencesStore.setState({
      loaded: true,
      tutorialStage: "run",
      tutorialSessionId: "sess-resume",
      tutorialRunId: "run-resume",
      tutorialSourceDataHash: "hash-resume",
    });
    render(<HelloWorldTutorial />);
    expect(
      await screen.findByRole("heading", { name: "This is the audit story." }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Exit tutorial" }),
    ).toBeInTheDocument();
  });

  it("offers no Exit tutorial control on the welcome bookend (Skip is the welcome exit)", () => {
    render(<HelloWorldTutorial />);
    expect(
      screen.getByRole("button", { name: "Skip the tutorial" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Exit tutorial" })).toBeNull();
  });

  it("offers no Exit tutorial control on graduation (the finish CTA is the exit)", () => {
    usePreferencesStore.setState({
      loaded: true,
      tutorialStage: "graduation",
      tutorialSessionId: "sess-resume",
      tutorialRunId: "run-resume",
      tutorialSourceDataHash: "hash-resume",
    });
    render(<HelloWorldTutorial />);
    expect(
      screen.getByRole("heading", { name: "You're ready to use the composer." }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Exit tutorial" })).toBeNull();
  });

  it("Exit tutorial during a freeform compose waits for the operation", async () => {
    const api = await import("@/api/client");
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    useSessionStore.setState({ isComposing: true });

    await user.click(await screen.findByRole("button", { name: "Exit tutorial" }));

    expect(await screen.findByRole("alert")).toHaveTextContent("current Composer operation");
    expect(api.updateUserComposerPreferences).not.toHaveBeenCalledWith(
      expect.objectContaining({ tutorial_completed_via: "exit" }),
    );
  });

  it("adds the renamed tutorial session to the header session list before Build", async () => {
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);

    await user.click(screen.getByRole("button", { name: "Let's go" }));

    await waitFor(() => {
      expect(useSessionStore.getState().sessions).toEqual(
        expect.arrayContaining([
          expect.objectContaining({
            id: "sess-new",
            title: "First-run tutorial (in progress)",
          }),
        ]),
      );
    });
  });

  it("Exit tutorial during an in-flight run aborts the fetch and fires the server-side cancel", async () => {
    const api = await import("@/api/client");
    // Distinct session id: the run cache is module-level (see the stub note).
    stubBuildSessionId = "sess-exit-mid-run";
    // A run that never settles — the exit must not depend on it finishing.
    vi.mocked(api.runTutorialPipeline).mockImplementationOnce(
      () => new Promise(() => {}),
    );
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    await user.click(
      await screen.findByRole("button", { name: "finish-build" }),
    );
    await clickRun(user);
    await user.click(screen.getByRole("button", { name: "Exit tutorial" }));

    const [, signal] = vi.mocked(api.runTutorialPipeline).mock.calls[0];
    expect((signal as AbortSignal).aborted).toBe(true);
    expect(api.cancelTutorialRun).toHaveBeenCalledWith("sess-exit-mid-run");
  });

  it("Exit tutorial from the pre-run card starts no run and still sends the idempotent cancel", async () => {
    // Before the Run click there is no request to abort, but the in-page
    // state cannot tell a fresh pre-run card from a reload that interrupted
    // a run still executing server-side (the persisted stage carries no run
    // identity until the result lands). The cancel endpoint is idempotent,
    // so Exit always sends it from the run stage: a no-op here, and the only
    // thing that stops a leaked run after a reload (red-team F3, 2026-09-02).
    const api = await import("@/api/client");
    stubBuildSessionId = "sess-exit-pre-run";
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    await user.click(
      await screen.findByRole("button", { name: "finish-build" }),
    );
    await screen.findByRole("button", { name: "Run" });
    await user.click(screen.getByRole("button", { name: "Exit tutorial" }));

    expect(api.runTutorialPipeline).not.toHaveBeenCalled();
    expect(api.cancelTutorialRun).toHaveBeenCalledWith("sess-exit-pre-run");
    await waitFor(() =>
      expect(usePreferencesStore.getState().tutorialCompleted).toBe(true),
    );
  });

  it("Exit tutorial after the run completes does not cancel the finished run", async () => {
    const api = await import("@/api/client");
    stubBuildSessionId = "sess-exit-after-run";
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    await user.click(
      await screen.findByRole("button", { name: "finish-build" }),
    );
    await clickRun(user);
    expect(await screen.findByText("bold")).toBeInTheDocument();
    await user.click(screen.getByRole("button", { name: "Exit tutorial" }));

    expect(api.cancelTutorialRun).not.toHaveBeenCalled();
  });
});

describe("HelloWorldTutorial — server-persisted resume (elspeth-918f4434b3)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
    resetStore(usePreferencesStore);
    stubBuildSessionId = "sess-new";
  });

  it("persists the Build stage + session on Start", async () => {
    const api = await import("@/api/client");
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    await waitFor(() =>
      expect(api.updateUserComposerPreferences).toHaveBeenCalledWith({
        tutorial_stage: "build",
        tutorial_session_id: "sess-new",
        tutorial_run_id: null,
        tutorial_source_data_hash: null,
      }),
    );
  });

  it("persists the run stage when Build completes", async () => {
    const api = await import("@/api/client");
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Let's go" }));
    await user.click(
      await screen.findByRole("button", { name: "finish-build" }),
    );
    await waitFor(() =>
      expect(api.updateUserComposerPreferences).toHaveBeenCalledWith(
        expect.objectContaining({
          tutorial_stage: "run",
          tutorial_session_id: "sess-new",
        }),
      ),
    );
  });

  it("persists the skip opt-out IMMEDIATELY on the first click", async () => {
    const api = await import("@/api/client");
    const user = userEvent.setup();
    render(<HelloWorldTutorial />);
    await user.click(screen.getByRole("button", { name: "Skip the tutorial" }));
    // The opt-out lands on the FIRST click — not on the follow-up
    // "Take me to the composer" click (which is never made here).
    await waitFor(() =>
      expect(api.updateUserComposerPreferences).toHaveBeenCalledWith({
        tutorial_completed_at: expect.any(String),
        tutorial_completed_via: "skip",
      }),
    );
    // No stage write for the skip path: the completion persist above
    // clears the resume state server-side.
    const bodies = vi.mocked(api.updateUserComposerPreferences).mock.calls.map(
      ([body]) => body,
    );
    expect(bodies.some((body) => "tutorial_stage" in body)).toBe(false);
    // The graduation card stays mounted (publishLocally=false): the skip
    // must not yank the farewell out from under the user.
    expect(
      screen.getByRole("heading", { name: "You're ready to use the composer." }),
    ).toBeInTheDocument();
  });

  it("resumes at freeform Build with the persisted session — no orphan sweep, no restart", async () => {
    const api = await import("@/api/client");
    usePreferencesStore.setState({
      loaded: true,
      tutorialStage: "build",
      tutorialSessionId: "sess-resume",
    });
    render(<HelloWorldTutorial />);
    // The Build shell (stubbed) mounts directly — not the Welcome bookend.
    expect(
      screen.getByRole("button", { name: "finish-build" }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("heading", { name: /welcome/i })).toBeNull();
    // The resumable session must NOT be renamed "abandoned-..." on reload:
    // orphan cleanup is a fresh-entry-only sweep.
    expect(api.deleteTutorialOrphans).not.toHaveBeenCalled();
    // And no session re-creation — the persisted session is reused.
    expect(api.createSession).not.toHaveBeenCalled();
  });

  it("falls back to a fresh Welcome when the resumed session no longer exists", async () => {
    // The persisted resume fields can outlive their session (orphan sweep,
    // archive, prerelease wipe). Without recovery the Build stage dead-ends
    // on "Session not found" with NO affordance (skip/exit are suppressed
    // past Welcome) — the operator-observed blank-page failure.
    const api = await import("@/api/client");
    // Once, not a standing override: clearAllMocks clears CALLS but keeps
    // implementations, so a standing mockResolvedValue([]) here would poison
    // the sibling resume tests into the recovery path.
    vi.mocked(api.fetchSessions).mockResolvedValueOnce([]);
    usePreferencesStore.setState({
      loaded: true,
      tutorialStage: "build",
      tutorialSessionId: "sess-resume",
    });
    render(<HelloWorldTutorial />);
    // Recovery lands on the Welcome bookend, not the Build shell.
    expect(
      await screen.findByRole("heading", { name: /welcome/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "finish-build" })).toBeNull();
    // And the stale server-side resume fields are cleared (welcome → all-null).
    await waitFor(() => {
      const bodies = vi
        .mocked(api.updateUserComposerPreferences)
        .mock.calls.map(([body]) => body);
      expect(
        bodies.some(
          (body) =>
            "tutorial_stage" in body && body.tutorial_stage === null,
        ),
      ).toBe(true);
    });
  });

  it("recovery is idempotent per dead id and releases the app-level session binding", async () => {
    // Live-observed residue of the dead-resume recovery: the shell's
    // sample-load 404 handler and the mount-time membership check RACE and
    // both detect the same dead session — the warning double-logged — and
    // activeSessionId stayed bound to the corpse, so InlineRunResults kept
    // polling /runs (404) while the user sat at Welcome.
    const { useSessionStore } = await import("@/stores/sessionStore");
    const warnSpy = vi
      .spyOn(console, "warn")
      .mockImplementation(() => undefined);
    stubShellReportsSessionMissing = true;
    useSessionStore.setState({ activeSessionId: "sess-resume" });
    usePreferencesStore.setState({
      loaded: true,
      tutorialStage: "build",
      tutorialSessionId: "sess-resume",
    });
    render(<HelloWorldTutorial />);
    expect(
      await screen.findByRole("heading", { name: /welcome/i }),
    ).toBeInTheDocument();
    // The dead binding is released — pollers keyed on activeSessionId stop.
    expect(useSessionStore.getState().activeSessionId).toBeNull();
    // Double detection of the same dead id recovers ONCE.
    const recoveryWarns = warnSpy.mock.calls.filter(([msg]) =>
      String(msg).includes("persisted resume session no longer exists"),
    );
    expect(recoveryWarns).toHaveLength(1);
    warnSpy.mockRestore();
  });

  it("resumes a completed run at the audit step without re-executing", async () => {
    const api = await import("@/api/client");
    usePreferencesStore.setState({
      loaded: true,
      tutorialStage: "run",
      tutorialSessionId: "sess-resume",
      tutorialRunId: "run-resume",
      tutorialSourceDataHash: "hash-resume",
    });
    render(<HelloWorldTutorial />);
    expect(
      await screen.findByRole("heading", { name: "This is the audit story." }),
    ).toBeInTheDocument();
    // Zero re-execution: the run identity came from the persisted fields.
    expect(api.runTutorialPipeline).not.toHaveBeenCalled();
    // The resumed audit suppresses Back — there is no in-memory run cache,
    // so Back into the run turn would silently re-fire the pipeline.
    await waitFor(() =>
      expect(screen.getByRole("button", { name: "Continue" })).toBeInTheDocument(),
    );
    expect(screen.queryByRole("button", { name: /back/i })).toBeNull();
  });

  it("resumes a run stage with no run identity at the Run button — nothing executes, the workspace re-binds", async () => {
    // A reload before the Run click (or mid-run — the persisted fields
    // cannot tell the two apart) lands back on the pre-run card: the run
    // must never auto-start on the learner's behalf (I-1). The store is
    // empty after a reload, so the run stage re-binds it through ordinary
    // session selection, without replaying the Build prompt.
    const api = await import("@/api/client");
    const selectSession = vi.fn(async (id: string) => {
      useSessionStore.setState({ activeSessionId: id, compositionStateLoaded: true });
    });
    useSessionStore.setState({ activeSessionId: null, selectSession });
    usePreferencesStore.setState({
      loaded: true,
      tutorialStage: "run",
      tutorialSessionId: "sess-resume",
    });
    render(<HelloWorldTutorial />);
    expect(
      await screen.findByRole("heading", { name: /ready to run/i }),
    ).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Run" })).toBeInTheDocument();
    expect(api.runTutorialPipeline).not.toHaveBeenCalled();
    // Bound and hydrated for the graph pane — read-only, no prompt or run.
    expect(useSessionStore.getState().activeSessionId).toBe("sess-resume");
    expect(selectSession).toHaveBeenCalledWith("sess-resume");
    // No stage write: the persisted `run` stage already matches.
    expect(api.updateUserComposerPreferences).not.toHaveBeenCalled();
  });

  it("resumes at graduation once the graduation card has been shown", async () => {
    usePreferencesStore.setState({
      loaded: true,
      tutorialStage: "graduation",
      tutorialSessionId: "sess-resume",
      tutorialRunId: "run-resume",
      tutorialSourceDataHash: "hash-resume",
    });
    render(<HelloWorldTutorial />);
    expect(
      screen.getByRole("heading", { name: "You're ready to use the composer." }),
    ).toBeInTheDocument();
  });

  it("does not fire a progress write when the persisted state already matches", async () => {
    const api = await import("@/api/client");
    usePreferencesStore.setState({
      loaded: true,
      tutorialStage: "graduation",
      tutorialSessionId: "sess-resume",
      tutorialRunId: "run-resume",
      tutorialSourceDataHash: "hash-resume",
    });
    render(<HelloWorldTutorial />);
    // The mount-time persist effect dedupes against the store's mirror of
    // the server row — a resume must not immediately re-PATCH it.
    await waitFor(() =>
      expect(
        screen.getByRole("heading", { name: "You're ready to use the composer." }),
      ).toBeInTheDocument(),
    );
    expect(api.updateUserComposerPreferences).not.toHaveBeenCalled();
  });
});
