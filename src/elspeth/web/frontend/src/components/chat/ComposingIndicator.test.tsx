import { act, fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ComposingIndicator, formatElapsed } from "./ComposingIndicator";
import type {
  ComposerProgressSnapshot,
  CompositionState,
  ToolCall,
} from "@/types/api";
import { compositionStateAuthorityFields } from "@/test/composerFixtures";

function makeState(overrides: Partial<CompositionState> = {}): CompositionState {
  return {
    id: "state-1",
    ...compositionStateAuthorityFields,
    version: 1,
    sources: {},
    nodes: [],
    edges: [],
    outputs: [],
    metadata: { name: null, description: null },
    ...overrides,
  };
}

describe("ComposingIndicator", () => {
  it("renders backend composer progress when available", () => {
    const progress: ComposerProgressSnapshot = {
      session_id: "session-1",
      request_id: "message-1",
      phase: "using_tools",
      headline: "The model requested plugin schemas.",
      evidence: ["Checking available source, transform, and sink tools."],
      likely_next: "ELSPETH will use the schemas to choose a pipeline shape.",
      reason: null,
      updated_at: "2026-04-26T10:00:00Z",
    };

    render(
      <ComposingIndicator
        latestRequest="Exploit this HTML into JSON"
        compositionState={makeState()}
        composerProgress={progress}
      />,
    );

    expect(screen.getByText("Working on...")).toBeInTheDocument();
    expect(screen.getByText("The model requested plugin schemas.")).toBeInTheDocument();
    expect(screen.queryByText("What ELSPETH can see")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show details" }));
    expect(screen.getByText("What ELSPETH can see")).toBeInTheDocument();
    expect(screen.getByText("Checking available source, transform, and sink tools.")).toBeInTheDocument();
    expect(screen.getByText("Likely next")).toBeInTheDocument();
    expect(screen.getByText("ELSPETH will use the schemas to choose a pipeline shape.")).toBeInTheDocument();
    expect(screen.queryByText("Working on: convert HTML into JSON")).not.toBeInTheDocument();
    // Backend-evidenced views must NOT carry the estimated marker
    // (elspeth-b189b5b3b8 part c).
    expect(screen.queryByText("(estimated)")).not.toBeInTheDocument();
    expect(screen.queryByText("Best guess from your request")).not.toBeInTheDocument();
  });

  it("shows a broad-strokes read of an HTML to JSON request", () => {
    render(
      <ComposingIndicator
        latestRequest="Exploit this HTML into JSON"
        compositionState={makeState()}
      />,
    );

    expect(screen.getByText("Working on...")).toBeInTheDocument();
    expect(screen.getByRole("status")).toHaveTextContent("Working on: convert HTML into JSON");
    expect(
      screen.queryByText("Request focus: turn HTML content into structured JSON."),
    ).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show details" }));
    expect(screen.getByText("Request focus: turn HTML content into structured JSON.")).toBeInTheDocument();
    expect(screen.getByText("Current setup: no input yet, no processing steps, no outputs.")).toBeInTheDocument();
    expect(
      screen.getByText("Likely next move: choose an input, extract the useful fields, then save structured JSON."),
    ).toBeInTheDocument();
  });

  it("marks keyword-guessed working views as estimated, distinct from backend evidence", () => {
    // elspeth-b189b5b3b8 part c: with no backend progress snapshot the view is
    // keyword-guessed from the user's message and must not read as if ELSPETH
    // reported it — visible "(estimated)" marker + the estimated section label
    // + an italicising modifier class the CSS hangs off.
    const { container } = render(
      <ComposingIndicator
        latestRequest="Exploit this HTML into JSON"
        compositionState={makeState()}
      />,
    );

    expect(screen.getByText("(estimated)")).toBeInTheDocument();
    expect(screen.queryByText("Best guess from your request")).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Show details" }));
    expect(screen.getByText("Best guess from your request")).toBeInTheDocument();
    expect(screen.queryByText("What ELSPETH can see")).not.toBeInTheDocument();
    expect(
      container.querySelector(".composing-working-view--estimated"),
    ).not.toBeNull();
  });

  it("summarizes existing pipeline shape without plugin jargon", () => {
    render(
      <ComposingIndicator
        latestRequest="Add an output file"
        compositionState={makeState({
          sources: {
            source: {
              plugin: "csv",
              options: {},
              on_success: "extract",
              on_validation_failure: "discard",
            },
          },
          nodes: [
            {
              id: "extract",
              node_type: "transform",
              plugin: "field_mapper",
              input: "source",
              on_success: null,
              on_error: null,
              options: {},
            },
          ],
          outputs: [{ name: "json_out", plugin: "json", options: {} }],
        })}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Show details" }));
    expect(screen.getByText("Current setup: input configured, 1 processing step, 1 output.")).toBeInTheDocument();
    expect(screen.getByText("Request focus: produce or update saved output.")).toBeInTheDocument();
  });

  it("keeps long-running details collapsed until requested", () => {
    render(
      <ComposingIndicator
        latestRequest="Save the rows to a JSON artifact"
        compositionState={makeState()}
      />,
    );

    expect(screen.getByText("Working on: saved output")).toBeInTheDocument();
    expect(screen.queryByText("Likely next")).not.toBeInTheDocument();

    const toggle = screen.getByRole("button", { name: "Show details" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    fireEvent.click(toggle);

    expect(screen.getByText("Likely next")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Hide details" }),
    ).toHaveAttribute("aria-expanded", "true");
  });

  it("keeps requested details open when the backend snapshot arrives for the same request", () => {
    const { rerender } = render(
      <ComposingIndicator
        latestRequest="Save the rows to a JSON artifact"
        compositionState={makeState()}
      />,
    );

    fireEvent.click(screen.getByRole("button", { name: "Show details" }));
    expect(screen.getByText("Likely next")).toBeInTheDocument();

    const progress: ComposerProgressSnapshot = {
      session_id: "session-1",
      request_id: "message-1",
      phase: "using_tools",
      headline: "Saving the JSON artifact.",
      evidence: ["Choosing the output sink."],
      likely_next: "ELSPETH will save the file.",
      reason: null,
      updated_at: "2026-04-26T10:00:00Z",
    };
    rerender(
      <ComposingIndicator
        latestRequest="Save the rows to a JSON artifact"
        compositionState={makeState()}
        composerProgress={progress}
      />,
    );

    expect(screen.getByText("What ELSPETH can see")).toBeInTheDocument();
    expect(
      screen.getByRole("button", { name: "Hide details" }),
    ).toHaveAttribute("aria-expanded", "true");
  });

  it("renders terminal progress as a retained last update", () => {
    const progress: ComposerProgressSnapshot = {
      session_id: "session-1",
      request_id: "message-1",
      phase: "cancelled",
      headline: "Composition stopped before saving.",
      evidence: ["The browser stopped the compose request."],
      likely_next: "Revise the request and send it again.",
      reason: "client_cancelled",
      updated_at: "2026-04-26T10:00:00Z",
    };

    render(<ComposingIndicator composerProgress={progress} />);

    expect(screen.getByText("Last composer update")).toBeInTheDocument();
    expect(screen.getByText("Stopped")).toBeInTheDocument();
    expect(screen.getByText("Composition stopped before saving.")).toBeInTheDocument();
    expect(screen.getByRole("status")).not.toHaveTextContent("Working on...");
    expect(screen.getByRole("status")).not.toHaveTextContent(/\bok\b/i);
  });
});

describe("ComposingIndicator elapsed-time readout", () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });

  afterEach(() => {
    vi.useRealTimers();
  });

  it("counts up in mm:ss while composing and is hidden from assistive tech", () => {
    // elspeth-b189b5b3b8 part a: slow must not read identically to stalled.
    const { container } = render(<ComposingIndicator latestRequest="hello" />);

    const readout = container.querySelector(".composing-elapsed");
    expect(readout).not.toBeNull();
    expect(readout?.textContent).toBe("00:00");
    // The once-per-second tick must not spam the role="status" live region.
    expect(readout?.getAttribute("aria-hidden")).toBe("true");

    act(() => {
      vi.advanceTimersByTime(65_000);
    });
    expect(container.querySelector(".composing-elapsed")?.textContent).toBe("01:05");
  });

  it("drops the readout once a terminal phase lands", () => {
    const progress: ComposerProgressSnapshot = {
      session_id: "session-1",
      request_id: "message-1",
      phase: "complete",
      headline: "Pipeline saved.",
      evidence: ["Saved version 3."],
      likely_next: null,
      reason: null,
      updated_at: "2026-04-26T10:00:00Z",
    };

    const { container } = render(<ComposingIndicator composerProgress={progress} />);
    expect(container.querySelector(".composing-elapsed")).toBeNull();
  });
});

describe("formatElapsed", () => {
  it("formats seconds as zero-padded mm:ss", () => {
    expect(formatElapsed(0)).toBe("00:00");
    expect(formatElapsed(9)).toBe("00:09");
    expect(formatElapsed(65)).toBe("01:05");
    expect(formatElapsed(600)).toBe("10:00");
  });

  it("clamps negative input to zero rather than rendering nonsense", () => {
    expect(formatElapsed(-5)).toBe("00:00");
  });
});

describe("ComposingIndicator terminal badge honesty (elspeth-bf9c296ee5)", () => {
  function completeProgress(): ComposerProgressSnapshot {
    return {
      session_id: "session-1",
      request_id: "message-1",
      phase: "complete",
      headline: "Composer finished.",
      evidence: ["Saved version 3."],
      likely_next: null,
      reason: "composer_complete",
      updated_at: "2026-04-26T10:00:00Z",
    };
  }

  it.each([
    ["response_ready", "Response ready"],
    ["pipeline_updated", "Pipeline updated"],
    ["review_required", "Review required"],
    ["pipeline_ready", "Pipeline ready"],
  ] as const)(
    "a complete phase with outcome %s renders the %s badge",
    (outcome, label) => {
      const { container } = render(
        <ComposingIndicator
          composerProgress={completeProgress()}
          completionOutcome={outcome}
        />,
      );
      expect(
        container.querySelector(".composing-terminal-mark")?.textContent,
      ).toBe(label);
    },
  );

  it("falls back to the legacy 'Updated' badge when the outcome is unknown", () => {
    // Fixture tolerance / snapshot-only reloads: with no derivable outcome the
    // badge keeps its historical claim rather than inventing a new one.
    const { container } = render(
      <ComposingIndicator composerProgress={completeProgress()} />,
    );
    expect(
      container.querySelector(".composing-terminal-mark")?.textContent,
    ).toBe("Updated");
  });

  it("failed and cancelled phases keep their own labels regardless of outcome", () => {
    const failed: ComposerProgressSnapshot = {
      ...completeProgress(),
      phase: "failed",
      reason: "plugin_crash",
    };
    const { container, rerender } = render(
      <ComposingIndicator
        composerProgress={failed}
        completionOutcome="pipeline_ready"
      />,
    );
    expect(
      container.querySelector(".composing-terminal-mark")?.textContent,
    ).toBe("Failed");

    const cancelled: ComposerProgressSnapshot = {
      ...completeProgress(),
      phase: "cancelled",
      reason: "client_cancelled",
    };
    rerender(
      <ComposingIndicator
        composerProgress={cancelled}
        completionOutcome="pipeline_ready"
      />,
    );
    expect(
      container.querySelector(".composing-terminal-mark")?.textContent,
    ).toBe("Stopped");
  });
});

describe("ComposingIndicator live tool log (elspeth-3c2caf56a7)", () => {
  function makeToolCall(
    id: string,
    name: string,
    outcome?: ToolCall["outcome"],
  ): ToolCall {
    return {
      id,
      type: "function",
      function: { name, arguments: "{}" },
      ...(outcome !== undefined ? { outcome } : {}),
    };
  }

  it("renders one always-visible entry per live call with outcome-honest prefixes", () => {
    const { container } = render(
      <ComposingIndicator
        latestRequest="build a pipeline"
        liveToolCalls={[
          makeToolCall("tc-1", "get_plugin_schema"),
          makeToolCall("tc-2", "set_pipeline"),
          makeToolCall("tc-3", "set_source", "applied"),
          makeToolCall("tc-4", "preview_pipeline", "failed"),
        ]}
      />,
    );

    // Visible WITHOUT opening the Show-details disclosure.
    expect(screen.getByRole("button", { name: "Show details" })).toBeInTheDocument();
    const log = container.querySelector(".composing-tool-log");
    expect(log).not.toBeNull();
    expect(log!.querySelectorAll("li")).toHaveLength(4);
    // No stamp: conservative lookup label for discovery tools, neutral
    // Running for mutating tools — never a fabricated "Applied".
    expect(screen.getByText("Looked up: get_plugin_schema")).toBeInTheDocument();
    expect(screen.getByText("Running: set_pipeline")).toBeInTheDocument();
    expect(screen.queryByText("Applied: set_pipeline")).not.toBeInTheDocument();
    // Server-stamped outcomes render their real verdicts.
    expect(screen.getByText("Applied: set_source")).toBeInTheDocument();
    expect(screen.getByText("Failed: preview_pipeline")).toBeInTheDocument();
    // The human description is VISIBLE, not bound to a mouse-only `title`
    // (ux-review 2026-08-13): during a multi-minute turn this log is the only
    // progress affordance, and a `title` is unreachable by keyboard, by touch,
    // and by most screen readers.
    const lookupEntry = screen.getByText("Looked up: get_plugin_schema")
      .parentElement!;
    expect(lookupEntry.tagName).toBe("LI");
    expect(lookupEntry).not.toHaveAttribute("title");
    expect(lookupEntry.textContent).toContain(
      "Reads a plugin's configuration schema to understand its options.",
    );
    // The description is the PRIMARY line and the machine identifier the
    // secondary one.
    expect(
      lookupEntry.querySelector(".composing-tool-log-what")!.textContent,
    ).toBe("Reads a plugin's configuration schema to understand its options.");
    expect(
      lookupEntry.querySelector(".composing-tool-log-call")!.textContent,
    ).toBe("Looked up: get_plugin_schema");
  });

  it("never lets a visible description swallow a failure prefix", () => {
    // The fabrication hazard of promoting the description: rendered alone,
    // "Sets the pipeline's data source…" claims a mutation that FAILED or was
    // rejected actually happened. The evidential prefix must survive on every
    // outcome, alongside the description.
    render(
      <ComposingIndicator
        latestRequest="build a pipeline"
        liveToolCalls={[
          makeToolCall("tc-1", "set_source", "failed"),
          makeToolCall("tc-2", "upsert_node", "rejected"),
          makeToolCall("tc-3", "remove_node", "cancelled"),
        ]}
      />,
    );

    for (const [label, description] of [
      [
        "Failed: set_source",
        "Sets the pipeline's data source — what records the pipeline starts from.",
      ],
      [
        "Attempted: upsert_node (not applied)",
        "Adds a new transform or gate node, or replaces an existing one with the same id.",
      ],
      [
        "Cancelled: remove_node",
        "Removes a transform or gate node from the pipeline.",
      ],
    ] as const) {
      const entry = screen.getByText(label).parentElement!;
      expect(entry.textContent).toContain(description);
      expect(entry.textContent).toContain(label);
    }
  });

  it("keeps an unmapped tool name to its bare label — no empty description line", () => {
    // describeToolCall's fallback is the generic "Composer tool call.", which
    // says nothing; a line carrying it would be noise, so unmapped names keep
    // exactly today's rendering.
    const { container } = render(
      <ComposingIndicator
        latestRequest="build a pipeline"
        liveToolCalls={[makeToolCall("tc-1", "some_future_tool")]}
      />,
    );

    const entry = container.querySelector(".composing-tool-log li")!;
    expect(entry.textContent).toBe("Running: some_future_tool");
    expect(entry.querySelector(".composing-tool-log-what")).toBeNull();
    expect(entry.textContent).not.toContain("Composer tool call.");
  });

  it("gives the log an accessible name so it is not an unlabelled sequence", () => {
    // Nothing told the operator what "Running: set_source" was a list OF.
    const { container } = render(
      <ComposingIndicator
        latestRequest="build a pipeline"
        liveToolCalls={[makeToolCall("tc-1", "set_source")]}
      />,
    );

    const log = container.querySelector(".composing-tool-log")!;
    const captionId = log.getAttribute("aria-labelledby");
    expect(captionId).not.toBeNull();
    const caption = document.getElementById(captionId!);
    expect(caption?.textContent).toBe("Composer actions in this turn");
    // Visible, not sr-only: it doubles as the plain-language anchor for the
    // Ran / Running / Looked up vocabulary.
    expect(caption?.classList.contains("visually-hidden")).toBe(false);
  });

  it("keeps the log OUTSIDE the role=status live region so appends never announce", () => {
    const { container } = render(
      <ComposingIndicator
        latestRequest="build a pipeline"
        liveToolCalls={[makeToolCall("tc-1", "get_pipeline_state")]}
      />,
    );

    const log = container.querySelector(".composing-tool-log");
    expect(log).not.toBeNull();
    expect(screen.getByRole("status").contains(log)).toBe(false);
    // No aria-live ancestor at all — an append-per-poll list inside a polite
    // region would announce every 1.5s tick (WCAG 4.1.3).
    for (
      let node = log!.parentElement;
      node !== null;
      node = node.parentElement
    ) {
      expect(node.getAttribute("aria-live")).toBeNull();
      expect(node.getAttribute("role")).not.toBe("status");
    }
  });

  it("renders no list at all when there are no live calls", () => {
    const { container } = render(
      <ComposingIndicator latestRequest="build a pipeline" />,
    );
    expect(container.querySelector(".composing-tool-log")).toBeNull();
  });
});

describe("ComposingIndicator live region scope", () => {
  it("keeps role=status on a non-interactive summary subregion", () => {
    // The indicator is mounted OUTSIDE ChatPanel's role="log" container
    // (elspeth-76a0cc485e) so its implicit role="status" politeness is the
    // single live region announcing compose progress. ChatPanel.test.tsx pins
    // the outside-the-log placement; this pins the region's own attributes.
    const { container } = render(<ComposingIndicator />);
    const root = container.firstChild as HTMLElement;
    const status = screen.getByRole("status");
    expect(root.getAttribute("role")).toBeNull();
    expect(status.getAttribute("aria-live")).toBeNull();
    expect(status.querySelector("button")).toBeNull();
  });
});
