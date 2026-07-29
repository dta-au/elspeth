import { render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import * as api from "@/api/client";
import {
  TUTORIAL_ASSUMPTION_CALLOUT,
  TUTORIAL_SHIELD_OVERRIDE_CAVEAT,
  TUTORIAL_SHIELD_WIRED_NOTE,
} from "./copy";
import { TutorialTurn4Run } from "./TutorialTurn4Run";
import { TutorialTurn5AuditStory } from "./TutorialTurn5AuditStory";

vi.mock("@/api/client", () => ({
  runTutorialPipeline: vi.fn(),
  getRunAuditSummary: vi.fn(),
  fetchPluginPolicy: vi.fn(),
}));

/** Policy snapshot whose prompt-shield capability is/is not selected. */
function policyWithShield(selected: boolean) {
  return {
    data: {
      selections: [
        {
          capability: "prompt_shield",
          plugin_id: selected ? "transform:aws_bedrock_prompt_shield" : null,
        },
      ],
    },
    snapshotFingerprint: "fp-1",
  } as unknown as Awaited<ReturnType<typeof api.fetchPluginPolicy>>;
}

function stubRun(): void {
  vi.mocked(api.runTutorialPipeline).mockResolvedValue({
    run_id: "run-1",
    output: { rows: [], source_data_hash: "h", discarded_row_count: 0 },
  } as unknown as Awaited<ReturnType<typeof api.runTutorialPipeline>>);
}

function noop(): void {}

describe("tutorial teaching moments — static copy", () => {
  it("the assumption callout names the reviewable inference", () => {
    // Must teach that an LLM transform makes REVIEWABLE assumptions you can
    // correct — not "assumptions are fine, ignore them".
    expect(TUTORIAL_ASSUMPTION_CALLOUT.toLowerCase()).toContain("assum");
    expect(TUTORIAL_ASSUMPTION_CALLOUT.toLowerCase()).toContain("review");
  });

  it("the shield-override caveat scopes the override to controlled inputs", () => {
    // The override is acceptable ONLY because we control the inputs — it must
    // NOT read as a general "skip the shield" habit.
    const lower = TUTORIAL_SHIELD_OVERRIDE_CAVEAT.toLowerCase();
    expect(lower).toContain("control");
    expect(lower).toContain("synthetic");
    expect(lower).toContain("high-risk");
  });
});

describe("tutorial teaching moments — render at the right turn", () => {
  beforeEach(() => {
    vi.clearAllMocks();
  });

  it("Turn 5 (audit story) renders the assumption callout", async () => {
    // Realistic RunAuditStoryResponse shape: once it resolves non-null the
    // component renders the summary-gated block (TutorialTurn5AuditStory.tsx:64-92),
    // which reads source_data_hash / llm_call_count / run_id / started_at and
    // calls formatPluginVersions(summary.plugin_versions) -> Object.entries(...).
    // plugin_versions MUST be an object ({}), not undefined, or Object.entries
    // throws a TypeError at render and masks the callout assertion.
    vi.mocked(api.getRunAuditSummary).mockResolvedValue({
      source_data_hash: "abc123",
      llm_call_count: 1,
      run_id: "r1",
      started_at: new Date().toISOString(),
      plugin_versions: {},
    } as unknown as Awaited<ReturnType<typeof api.getRunAuditSummary>>);
    render(
      <TutorialTurn5AuditStory
        sessionId="sess-1"
        runId="run-1"
        onContinue={noop}
        onBack={noop}
      />,
    );
    await waitFor(() =>
      expect(screen.getByText(TUTORIAL_ASSUMPTION_CALLOUT)).toBeInTheDocument(),
    );
    expect(screen.queryByText(/received its score/i)).not.toBeInTheDocument();
    expect(screen.getByText(/what it chose to include or leave out/i)).toBeInTheDocument();
    // The Back button here really does return to the run-results view
    // (previousStep(audit) === "run", cache-backed re-view) — not a
    // free-text prompt editor, which the staged guided walk retired (F6).
    const backButton = screen.getByRole("button", {
      name: "Back to your pipeline run",
    });
    expect(backButton).toBeInTheDocument();
    expect(backButton).not.toHaveAccessibleName(/edit prompt/i);
  });

  it("Turn 4 (run) renders the shield-override caveat with no shield selected", async () => {
    stubRun();
    vi.mocked(api.fetchPluginPolicy).mockResolvedValue(policyWithShield(false));
    render(
      <TutorialTurn4Run
        sessionId="sess-shield-absent"
        onCompleted={noop}
        onCancelled={noop}
      />,
    );
    expect(
      await screen.findByText(TUTORIAL_SHIELD_OVERRIDE_CAVEAT),
    ).toBeInTheDocument();
  });

  it("Turn 4 (run) never claims an unshielded run when a shield is selected", async () => {
    // The caveat asserts a FACT about the running pipeline. Once the composer
    // began wiring an available shield instead of recommending it, rendering
    // this string over a shielded run told the user something demonstrably
    // untrue about their own pipeline.
    stubRun();
    vi.mocked(api.fetchPluginPolicy).mockResolvedValue(policyWithShield(true));
    render(
      <TutorialTurn4Run
        sessionId="sess-shield-present"
        onCompleted={noop}
        onCancelled={noop}
      />,
    );
    expect(await screen.findByText(TUTORIAL_SHIELD_WIRED_NOTE)).toBeInTheDocument();
    expect(screen.queryByText(TUTORIAL_SHIELD_OVERRIDE_CAVEAT)).toBeNull();
  });

  it("Turn 4 (run) states neither shield fact when the policy is unreadable", async () => {
    // Showing nothing costs a teaching moment; guessing states a falsehood
    // about the user's own pipeline. The run itself must still proceed.
    stubRun();
    vi.mocked(api.fetchPluginPolicy).mockRejectedValue(new Error("policy down"));
    render(
      <TutorialTurn4Run
        sessionId="sess-policy-down"
        onCompleted={noop}
        onCancelled={noop}
      />,
    );
    await waitFor(() => {
      expect(api.runTutorialPipeline).toHaveBeenCalled();
    });
    expect(screen.queryByText(TUTORIAL_SHIELD_OVERRIDE_CAVEAT)).toBeNull();
    expect(screen.queryByText(TUTORIAL_SHIELD_WIRED_NOTE)).toBeNull();
  });
});
