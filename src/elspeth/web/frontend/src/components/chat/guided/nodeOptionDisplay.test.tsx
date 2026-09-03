import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import {
  NodeOptionsSummary,
  PROMPT_COLLAPSE_CHARS,
  PROMPT_COLLAPSE_LINES,
  collapsedPromptText,
  isPromptOption,
  nodeOptionText,
  promptNeedsCollapse,
} from "./nodeOptionDisplay";

const LONG_PROMPT = Array.from(
  { length: PROMPT_COLLAPSE_LINES + 4 },
  (_, index) => `Line ${index + 1} of the instruction.`,
).join("\n");

describe("nodeOptionDisplay helpers", () => {
  it("labels the llm decision inputs in the approval card's vocabulary, other keys by their token", () => {
    expect(nodeOptionText({ key: "model", value: "anthropic/claude-sonnet-4" })).toBe(
      "Model: anthropic/claude-sonnet-4",
    );
    expect(nodeOptionText({ key: "system_prompt", value: "Be terse." })).toBe("System prompt: Be terse.");
    expect(nodeOptionText({ key: "prompt_template", value: "Rate it." })).toBe("Prompt: Rate it.");
    expect(nodeOptionText({ key: "mapping", value: "a → b" })).toBe("Mapping: a → b");
    // web_scrape's display-only `http` identity carries the backend-rendered text.
    expect(nodeOptionText({ key: "http", value: "contact: ops@example.org; reason: catalogue refresh" })).toBe(
      "Scraping identity (confirmed after commit): contact: ops@example.org; reason: catalogue refresh",
    );
    expect(nodeOptionText({ key: "select_only", value: "only the mapped fields are kept" })).toBe(
      "Select only: only the mapped fields are kept",
    );
  });

  it("recognises exactly the prompt keys as prose the user approves", () => {
    expect(isPromptOption({ key: "prompt_template", value: "x" })).toBe(true);
    expect(isPromptOption({ key: "system_prompt", value: "x" })).toBe(true);
    expect(isPromptOption({ key: "model", value: "x" })).toBe(false);
    expect(isPromptOption({ key: "mapping", value: "x" })).toBe(false);
  });

  it("collapses a prompt only past the line or character threshold, keeping the first lines", () => {
    expect(promptNeedsCollapse("one line")).toBe(false);
    const atLimit = Array.from({ length: PROMPT_COLLAPSE_LINES }, () => "l").join("\n");
    expect(promptNeedsCollapse(atLimit)).toBe(false);
    expect(promptNeedsCollapse(`${atLimit}\nl`)).toBe(true);
    expect(promptNeedsCollapse("x".repeat(PROMPT_COLLAPSE_CHARS))).toBe(false);
    expect(promptNeedsCollapse("x".repeat(PROMPT_COLLAPSE_CHARS + 1))).toBe(true);

    expect(collapsedPromptText(LONG_PROMPT)).toBe(
      LONG_PROMPT.split("\n").slice(0, PROMPT_COLLAPSE_LINES).join("\n"),
    );
    const oneLongLine = "y".repeat(PROMPT_COLLAPSE_CHARS + 40);
    expect(collapsedPromptText(oneLongLine)).toBe("y".repeat(PROMPT_COLLAPSE_CHARS));
  });
});

describe("NodeOptionsSummary", () => {
  it("renders knob pairs as the plain labelled lines they always were, with no Edit", () => {
    render(
      <NodeOptionsSummary
        entries={[
          { key: "mapping", value: "a → b" },
          { key: "select_only", value: "only the mapped fields are kept" },
        ]}
        nodeLabel="node-1"
        onEdit={vi.fn()}
      />,
    );
    expect(screen.getByText("Mapping: a → b")).toBeInTheDocument();
    expect(screen.getByText("Select only: only the mapped fields are kept")).toBeInTheDocument();
    // Edit is a prompt affordance — a knob-only summary never grows one.
    expect(screen.queryByRole("button")).toBeNull();
  });

  it("renders the model as a line and a short prompt in full, preserving line breaks, with no toggle", () => {
    render(
      <NodeOptionsSummary
        entries={[
          { key: "model", value: "anthropic/claude-sonnet-4", tier: "common" },
          { key: "prompt_template", value: "Summarise the text.\nOne sentence.", tier: "common" },
        ]}
        nodeLabel="node-1"
      />,
    );
    expect(screen.getByText("Model: anthropic/claude-sonnet-4")).toBeInTheDocument();
    const text = screen.getByText(/Summarise the text\./);
    expect(text).toHaveTextContent("Summarise the text. One sentence.");
    expect(text.className).toContain("guided-node-prompt__text");
    expect(screen.queryByRole("button", { name: /Show full prompt/ })).toBeNull();
  });

  it("shows the first lines of a long prompt behind an expandable toggle", async () => {
    const user = userEvent.setup();
    render(
      <NodeOptionsSummary entries={[{ key: "prompt_template", value: LONG_PROMPT }]} nodeLabel="node-1" />,
    );

    const toggle = screen.getByRole("button", { name: "Show full prompt for node-1" });
    expect(toggle).toHaveAttribute("aria-expanded", "false");
    expect(screen.getByText(/Line 1 of the instruction\./)).toBeInTheDocument();
    expect(screen.queryByText(/Line 10 of the instruction\./)).toBeNull();

    await user.click(toggle);
    expect(
      screen.getByRole("button", { name: "Show less of the prompt for node-1" }),
    ).toHaveAttribute("aria-expanded", "true");
    expect(screen.getByText(/Line 10 of the instruction\./)).toBeInTheDocument();
  });

  it("offers one Edit for the node's prompts only when the caller can route the change", async () => {
    const user = userEvent.setup();
    const onEdit = vi.fn();
    const entries = [
      { key: "model", value: "m" },
      { key: "system_prompt", value: "Be terse." },
      { key: "prompt_template", value: "Rate it." },
    ];
    const { rerender } = render(<NodeOptionsSummary entries={entries} nodeLabel="node-2" onEdit={onEdit} />);
    expect(screen.getAllByRole("button", { name: /Edit/ })).toHaveLength(1);
    await user.click(screen.getByRole("button", { name: "Edit prompt for node-2" }));
    expect(onEdit).toHaveBeenCalledTimes(1);

    rerender(<NodeOptionsSummary entries={entries} nodeLabel="node-2" />);
    expect(screen.queryByRole("button", { name: /Edit/ })).toBeNull();

    // Locked (mid-submit) reads as disabled, not absent — like the Revise
    // buttons it opens.
    rerender(<NodeOptionsSummary entries={entries} nodeLabel="node-2" onEdit={onEdit} editDisabled />);
    expect(screen.getByRole("button", { name: "Edit prompt for node-2" })).toBeDisabled();
    // A knob line is not a prompt: no Edit grows from it — including the
    // display-only scraping identity, which a correction can never touch.
    rerender(
      <NodeOptionsSummary
        entries={[{ key: "http", value: "contact: ops@example.org; reason: catalogue refresh" }]}
        nodeLabel="node-3"
        onEdit={onEdit}
      />,
    );
    expect(screen.getByText("Scraping identity (confirmed after commit): contact: ops@example.org; reason: catalogue refresh")).toBeInTheDocument();
    expect(screen.queryByRole("button")).toBeNull();
  });
});
