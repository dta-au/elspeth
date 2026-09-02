import { StrictMode } from "react";
import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/api/client";
import { TURN_4_RUN_BUTTON } from "./copy";
import { TutorialTurn4Run } from "./TutorialTurn4Run";

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

  it("clicking Run calls the run endpoint exactly once and notifies onRunStart", async () => {
    vi.mocked(api.runTutorialPipeline).mockResolvedValue(okRun("run-click"));
    const onRunStart = vi.fn();
    render(
      <TutorialTurn4Run
        sessionId="sess-run-click"
        onRunStart={onRunStart}
        onCompleted={noop}
        onCancelled={noop}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: TURN_4_RUN_BUTTON }));

    expect(onRunStart).toHaveBeenCalledTimes(1);
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
