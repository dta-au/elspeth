import { StrictMode } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/api/client";
import { TURN_4_RUN_BUTTON } from "./copy";
import { TutorialTurn4Run } from "./TutorialTurn4Run";

// The real step header (Run, Back, Continue) around the run's body.
vi.mock("./TutorialWorkspaceFrame", () => import("@/test/tutorialWorkspaceFrameStub"));
vi.mock("@/api/client", () => ({
  runTutorialPipeline: vi.fn(),
  cancelTutorialRun: vi.fn(),
  fetchPluginPolicy: vi.fn().mockResolvedValue({
    data: { selections: [], control_modes: [] },
    snapshotFingerprint: "fp",
  }),
}));

function noop(): void {}

function okRun(runId: string) {
  return {
    run_id: runId,
    output: {
      rows: [{ url: "a", summary: "s" }],
      source_data_hash: "h",
      discarded_row_count: 0,
    },
  };
}

// Distinct session ids per test: the run cache is module-level and keyed by
// sessionId, so a reused id would replay a previous test's run.

describe("TutorialTurn4Run — explicit Run button (I-1: the run never auto-fires)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("mounting the run turn does NOT call the run endpoint", async () => {
    vi.mocked(api.runTutorialPipeline).mockResolvedValue(okRun("run-mount"));
    render(
      <TutorialTurn4Run
        sessionId="sess-no-autofire"
        onCompleted={noop}
        onCancelled={noop}
      />,
    );

    // The pre-run card: a heading that does not claim a run is in progress,
    // the primary Run button, and no BUSY status (the privacy disclosure is
    // an AlertBanner, which is itself a non-busy role="status").
    expect(
      screen.getByRole("heading", { name: /ready to run/i }),
    ).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: TURN_4_RUN_BUTTON }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("status", { busy: true })).not.toBeInTheDocument();
    expect(screen.queryByText(/fetching pages/i)).not.toBeInTheDocument();
    // Settle any microtasks — still no run.
    await Promise.resolve();
    expect(api.runTutorialPipeline).not.toHaveBeenCalled();
  });

  it("clicking Run calls the run endpoint exactly once", async () => {
    vi.mocked(api.runTutorialPipeline).mockResolvedValue(okRun("run-click"));
    render(
      <TutorialTurn4Run
        sessionId="sess-run-click"
        onCompleted={noop}
        onCancelled={noop}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: TURN_4_RUN_BUTTON }));

    expect(
      screen.getByRole("heading", { name: /running your pipeline/i }),
    ).toBeInTheDocument();
    expect(await screen.findByText(/rows returned/i)).toBeInTheDocument();
    expect(api.runTutorialPipeline).toHaveBeenCalledTimes(1);
    expect(vi.mocked(api.runTutorialPipeline).mock.calls[0][0]).toEqual({
      session_id: "sess-run-click",
    });
    // The Run button is consumed by the click: no second Run affordance.
    expect(
      screen.queryByRole("button", { name: TURN_4_RUN_BUTTON }),
    ).not.toBeInTheDocument();
  });

  it("clicking Run under React StrictMode still runs exactly once", async () => {
    vi.mocked(api.runTutorialPipeline).mockResolvedValue(okRun("run-strict"));
    render(
      <StrictMode>
        <TutorialTurn4Run
          sessionId="sess-run-strict"
          onCompleted={noop}
          onCancelled={noop}
        />
      </StrictMode>,
    );

    expect(api.runTutorialPipeline).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", { name: TURN_4_RUN_BUTTON }));
    expect(await screen.findByText(/rows returned/i)).toBeInTheDocument();
    expect(api.runTutorialPipeline).toHaveBeenCalledTimes(1);
  });

  it("a remount after the run started re-attaches to the same run instead of offering Run again", async () => {
    // audit → Back → run: the run result is cache-backed and re-viewable. The
    // remount must not show the Run button (a second click would be a second
    // run) and must not fire a second request.
    vi.mocked(api.runTutorialPipeline).mockResolvedValue(okRun("run-reattach"));
    const first = render(
      <TutorialTurn4Run
        sessionId="sess-reattach"
        onCompleted={noop}
        onCancelled={noop}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: TURN_4_RUN_BUTTON }));
    expect(await screen.findByText(/rows returned/i)).toBeInTheDocument();
    first.unmount();

    render(
      <TutorialTurn4Run
        sessionId="sess-reattach"
        onCompleted={noop}
        onCancelled={noop}
      />,
    );
    expect(
      screen.queryByRole("button", { name: TURN_4_RUN_BUTTON }),
    ).not.toBeInTheDocument();
    expect(await screen.findByText(/rows returned/i)).toBeInTheDocument();
    expect(api.runTutorialPipeline).toHaveBeenCalledTimes(1);
  });

  it("the privacy preamble is shown BEFORE the run can start", () => {
    vi.mocked(api.runTutorialPipeline).mockResolvedValue(okRun("run-preamble"));
    render(
      <TutorialTurn4Run
        sessionId="sess-preamble"
        onCompleted={noop}
        onCancelled={noop}
      />,
    );
    // The disclosure names the network + LLM calls the Run click will cause;
    // it must be visible while the decision is still the learner's.
    expect(
      screen.getByText(/calls the configured LLM and fetches pages/i),
    ).toBeInTheDocument();
    expect(api.runTutorialPipeline).not.toHaveBeenCalled();
  });
});

describe("TutorialTurn4Run — the step header carries the run, the pane carries its body", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("keeps the title, instruction and actions out of the authoring pane through every phase", async () => {
    vi.mocked(api.runTutorialPipeline).mockResolvedValue(okRun("run-header"));
    const onCompleted = vi.fn();
    render(
      <TutorialTurn4Run
        sessionId="sess-run-header"
        onCompleted={onCompleted}
        onCancelled={noop}
        onBack={noop}
      />,
    );
    const pane = screen.getByTestId("authoring-pane");

    const ready = screen.getByRole("heading", { name: "Ready to run." });
    expect(ready).toHaveFocus();
    expect(pane).not.toContainElement(ready);
    expect(pane).not.toContainElement(screen.getByRole("button", { name: TURN_4_RUN_BUTTON }));
    expect(pane).not.toContainElement(screen.getByRole("button", { name: "Back" }));
    expect(pane).toHaveTextContent(/calls the configured LLM and fetches pages/i);

    fireEvent.click(screen.getByRole("button", { name: TURN_4_RUN_BUTTON }));
    const running = screen.getByRole("heading", { name: "Running your pipeline." });
    expect(running).toHaveFocus();
    const status = screen.getByRole("status", { busy: true });
    expect(pane).not.toContainElement(status);

    expect(await screen.findByRole("heading", { name: "Your pipeline ran." })).toBeInTheDocument();
    // The same live region now carries the outcome, and is no longer busy.
    expect(status).toHaveTextContent(/Done\. 1 rows returned\./);
    expect(status).not.toHaveAttribute("aria-busy");
    expect(pane).toContainElement(screen.getByRole("table"));
    const continueButton = screen.getByRole("button", { name: "Continue" });
    expect(pane).not.toContainElement(continueButton);
    fireEvent.click(continueButton);
    expect(onCompleted).toHaveBeenCalledTimes(1);
  });

  it("names a failed run in the title and keeps Retry in the header", async () => {
    vi.mocked(api.runTutorialPipeline).mockRejectedValue(new Error("The tutorial runner is unavailable."));
    render(
      <TutorialTurn4Run
        sessionId="sess-run-header-error"
        onCompleted={noop}
        onCancelled={noop}
        onBack={noop}
      />,
    );
    fireEvent.click(screen.getByRole("button", { name: TURN_4_RUN_BUTTON }));

    expect(await screen.findByRole("heading", { name: "The run did not complete." })).toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("The tutorial runner is unavailable.");
    expect(screen.getByText("Retry the run, or go back to Build.")).toBeInTheDocument();
    expect(screen.getByTestId("authoring-pane")).not.toContainElement(
      screen.getByRole("button", { name: "Retry" }),
    );
  });
});
