// GuidedHistory -- always-visible plain-language decision summary.

import { describe, expect, it } from "vitest";
import { render, screen } from "@testing-library/react";
import {
  GuidedHistory,
  projectCompletedGuidedHistory,
} from "./GuidedHistory";
import type { TurnRecord } from "@/types/guided";

const TURN_1: TurnRecord = {
  step: "step_1_source",
  turn_type: "single_select",
  payload_hash: "aabbcc001122",
  response_hash: "ddeeff334455",
  emitter: "server",
  summary: "Source selected: csv",
};

const TURN_2: TurnRecord = {
  step: "step_2_sink",
  turn_type: "schema_form",
  payload_hash: "112233aabbcc",
  response_hash: null,
  emitter: "llm",
  summary: "Sink configured: jsonl",
};

describe("projectCompletedGuidedHistory", () => {
  it("returns no rows when history contains only current-step records", () => {
    expect(
      projectCompletedGuidedHistory([TURN_1], "step_1_source"),
    ).toEqual([]);
  });

  it("returns no rows when prior-step records have no summary", () => {
    expect(
      projectCompletedGuidedHistory(
        [{ ...TURN_1, summary: null }],
        "step_2_sink",
      ),
    ).toEqual([]);
  });

  it("projects the latest summarised record for each completed step", () => {
    const latestSource = {
      ...TURN_1,
      turn_type: "schema_form" as const,
      summary: "Source configured: csv",
    };

    expect(
      projectCompletedGuidedHistory(
        [TURN_1, latestSource, TURN_2],
        "step_3_transforms",
      ),
    ).toEqual([latestSource, TURN_2]);
  });

  it("retains the final current-step decision once the guided session is terminal", () => {
    expect(
      projectCompletedGuidedHistory(
        [TURN_1, TURN_2],
        "step_2_sink",
        { kind: "completed", reason: null, pipeline_yaml: "source: {}" },
      ),
    ).toEqual([TURN_1, TURN_2]);
  });

  it("excludes the current-step decision after exiting to freeform mid-step", () => {
    expect(
      projectCompletedGuidedHistory(
        [TURN_1, TURN_2],
        "step_2_sink",
        { kind: "exited_to_freeform", reason: "user_pressed_exit", pipeline_yaml: null },
      ),
    ).toEqual([TURN_1]);
  });
});

describe("GuidedHistory", () => {
  it("renders an always-visible decision summary heading", () => {
    render(<GuidedHistory history={[TURN_1, TURN_2]} currentStep="step_4_wire" />);
    expect(
      screen.getByRole("heading", { name: /decisions so far/i }),
    ).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /show steps/i })).toBeNull();
  });

  it("renders one list item per completed decision", () => {
    render(<GuidedHistory history={[TURN_1, TURN_2]} currentStep="step_4_wire" />);
    expect(screen.getAllByRole("listitem")).toHaveLength(2);
    expect(screen.getByText("Source selected: csv")).toBeInTheDocument();
    expect(screen.getByText("Sink configured: jsonl")).toBeInTheDocument();
  });

  it("uses step labels and hides emitter details from the default surface", () => {
    render(<GuidedHistory history={[TURN_1]} currentStep="step_2_sink" />);
    expect(screen.getByText("Source")).toBeInTheDocument();
    expect(screen.queryByText("server")).toBeNull();
  });

  it("omits a step that has not yet recorded a summary (in progress)", () => {
    // currentStep is elsewhere, so the only thing keeping step_1_source out is
    // the missing summary. The whole card collapses to nothing — no "Decided".
    const { container } = render(
      <GuidedHistory
        history={[{ ...TURN_1, summary: null }]}
        currentStep="step_2_sink"
      />,
    );
    expect(container.firstChild).toBeNull();
    expect(screen.queryByText("Decided")).toBeNull();
  });

  it("does not render the current step even when it already carries a summary", () => {
    // Shot 02: still on Source, the answered single_select recorded a summary,
    // but the source schema_form is not yet submitted — Source is not decided.
    const { container } = render(
      <GuidedHistory
        history={[{ ...TURN_1, summary: "Configured: json" }]}
        currentStep="step_1_source"
      />,
    );
    expect(container.firstChild).toBeNull();
  });

  it("renders the final wiring decision for a terminal guided session", () => {
    render(
      <GuidedHistory
        history={[{
          ...TURN_2,
          step: "step_4_wire",
          summary: "Connected transform to output",
        }]}
        currentStep="step_4_wire"
        terminal={{
          kind: "completed",
          reason: null,
          pipeline_yaml: "source: {}",
        }}
      />,
    );

    expect(screen.getByText("Connected transform to output")).toBeInTheDocument();
  });

  it("does not render the current step after exiting to freeform mid-step", () => {
    const { container } = render(
      <GuidedHistory
        history={[TURN_2]}
        currentStep="step_2_sink"
        terminal={{
          kind: "exited_to_freeform",
          reason: "user_pressed_exit",
          pipeline_yaml: null,
        }}
      />,
    );

    expect(container.firstChild).toBeNull();
    expect(screen.queryByText("Sink configured: jsonl")).toBeNull();
  });

  it("renders only the completed rows selected by the shared projection", () => {
    render(
      <GuidedHistory
        history={[
          TURN_1,
          { ...TURN_2, summary: null },
          {
            ...TURN_2,
            step: "step_3_transforms",
            summary: "Current transform choice",
          },
        ]}
        currentStep="step_3_transforms"
      />,
    );

    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByText("Source selected: csv")).toBeInTheDocument();
    expect(screen.queryByText("Current transform choice")).toBeNull();
  });

  it("collapses multiple turns of the same step to one row (most-recent summary wins)", () => {
    render(
      <GuidedHistory
        history={[
          { ...TURN_1, summary: null },
          { ...TURN_1, turn_type: "schema_form", summary: "Source configured: csv" },
        ]}
        currentStep="step_2_sink"
      />,
    );
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByText("Source configured: csv")).toBeInTheDocument();
  });

  it("does not let a trailing unsummarised next-turn mask the step's summary", () => {
    // Real shape after a chat-resolve: the entry record carries the decision
    // summary, then an unanswered next-turn record (summary: null) is emitted.
    render(
      <GuidedHistory
        history={[
          { ...TURN_1, turn_type: "single_select", summary: "Configured: web_scrape" },
          { ...TURN_1, turn_type: "schema_form", summary: null },
        ]}
        currentStep="step_2_sink"
      />,
    );
    expect(screen.getAllByRole("listitem")).toHaveLength(1);
    expect(screen.getByText("Configured: web_scrape")).toBeInTheDocument();
    expect(screen.queryByText("Decided")).toBeNull();
  });

  it("renders nothing when history is empty", () => {
    const { container } = render(<GuidedHistory history={[]} currentStep="step_1_source" />);
    expect(container.firstChild).toBeNull();
  });
});
