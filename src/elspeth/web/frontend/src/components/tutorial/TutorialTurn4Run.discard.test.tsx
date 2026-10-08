import { fireEvent, render, screen } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/api/client";
import { TURN_4_RUN_BUTTON } from "./copy";
import { TutorialTurn4Run } from "./TutorialTurn4Run";

// The real step header around the run's body (results and discard note).
vi.mock("./TutorialWorkspaceFrame", () => import("@/test/tutorialWorkspaceFrameStub"));
vi.mock("@/api/client", () => ({
  runTutorialPipeline: vi.fn(),
  fetchPluginPolicy: vi.fn().mockResolvedValue({ data: { selections: [], control_modes: [] }, snapshotFingerprint: "fp" }),
}));

function noop(): void {}

/** The learner's explicit Run gesture (I-1): nothing runs on mount. */
function clickRun(): void {
  fireEvent.click(screen.getByRole("button", { name: TURN_4_RUN_BUTTON }));
}

describe("TutorialTurn4Run — source-discarded row surfacing (#28)", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("surfaces the discarded row count when the source dropped rows", async () => {
    vi.mocked(api.runTutorialPipeline).mockResolvedValue({
      run_id: "run-discard",
      output: {
        rows: [{ url: "a" }, { url: "b" }],
        source_data_hash: "h",
        discarded_row_count: 3,
      },
    });

    render(
      <TutorialTurn4Run
        sessionId="sess-discard"
        onCompleted={noop}
        onCancelled={noop}
        onBack={noop}
      />,
    );
    clickRun();

    expect(await screen.findByText(/3 rows were discarded at the source/i)).toBeInTheDocument();
  });

  it("shows no discard notice when nothing was dropped", async () => {
    vi.mocked(api.runTutorialPipeline).mockResolvedValue({
      run_id: "run-clean",
      output: {
        rows: [{ url: "a" }],
        source_data_hash: "h",
        discarded_row_count: 0,
      },
    });

    render(
      <TutorialTurn4Run
        sessionId="sess-clean"
        onCompleted={noop}
        onCancelled={noop}
        onBack={noop}
      />,
    );
    clickRun();

    expect(await screen.findByText(/rows returned/i)).toBeInTheDocument();
    expect(screen.queryByText(/discarded at the source/i)).not.toBeInTheDocument();
  });

  it("labels the completed-run Back button with real behaviour, not the retired prompt-editor copy", async () => {
    // Back revisits run results; it does not edit the Build prompt.
    vi.mocked(api.runTutorialPipeline).mockResolvedValue({
      run_id: "run-label",
      output: {
        rows: [{ url: "a" }],
        source_data_hash: "h",
        discarded_row_count: 0,
      },
    });

    render(
      <TutorialTurn4Run
        sessionId="sess-label"
        onCompleted={noop}
        onCancelled={noop}
        onBack={noop}
      />,
    );
    clickRun();

    await screen.findByText(/rows returned/i);
    const backButton = screen.getByRole("button", {
      name: "Back to the pipeline build",
    });
    expect(backButton).toBeInTheDocument();
    expect(backButton).not.toHaveAccessibleName(/edit prompt/i);
  });
});
