import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { describe, expect, it, vi } from "vitest";

import { ValidationResultBanner } from "./ValidationResult";
import type { ValidationResult } from "@/types/index";

const READY_READINESS = {
  authoring_valid: true,
  execution_ready: true,
  completion_ready: true,
  blockers: [],
};

function makePassResult(overrides: Partial<ValidationResult> = {}): ValidationResult {
  return {
    is_valid: true,
    summary: "Validation passed",
    checks: [
      {
        name: "plugin_enablement",
        passed: true,
        detail: "plugin_enablement passed.",
        affected_nodes: [],
        outcome_code: null,
      },
      {
        name: "graph_structure",
        passed: true,
        detail: "Graph structure is valid",
        affected_nodes: [],
        outcome_code: null,
      },
    ],
    errors: [],
    warnings: [],
    readiness: READY_READINESS,
    ...overrides,
  };
}

describe("ValidationResultBanner", () => {
  it("collapses a clean pass to a one-line summary without check details", () => {
    render(<ValidationResultBanner result={makePassResult()} />);

    const summaryBtn = screen.getByRole("button", {
      name: /validation passed\. show details\./i,
    });
    expect(summaryBtn).toHaveAttribute("aria-expanded", "false");
    expect(summaryBtn).toHaveTextContent("2 checks");
    expect(screen.queryByText(/plugin_enablement passed/)).toBeNull();
  });

  it("expands to check details on click and collapses again via Collapse", async () => {
    const user = userEvent.setup();
    render(<ValidationResultBanner result={makePassResult()} />);

    await user.click(
      screen.getByRole("button", { name: /validation passed\. show details\./i }),
    );

    expect(screen.getByText(/plugin_enablement passed/)).toBeInTheDocument();
    expect(screen.getByText(/Graph structure is valid/)).toBeInTheDocument();

    await user.click(
      screen.getByRole("button", { name: /collapse validation details/i }),
    );

    expect(screen.queryByText(/plugin_enablement passed/)).toBeNull();
    expect(
      screen.getByRole("button", { name: /validation passed\. show details\./i }),
    ).toBeInTheDocument();
  });

  it("auto-expands when the pass carries warnings and offers no Collapse", () => {
    render(
      <ValidationResultBanner
        result={makePassResult({
          warnings: [
            {
              component_id: "csv_source",
              component_type: "source",
              message: "Batch size is small",
              suggestion: "Consider increasing batch size",
            },
          ],
        })}
      />,
    );

    expect(screen.getByText(/Batch size is small/)).toBeInTheDocument();
    expect(screen.getByText(/plugin_enablement passed/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /collapse validation details/i }),
    ).toBeNull();
  });

  it("auto-expands when a passed check carries identity_node_advisory guidance and offers no Collapse", () => {
    render(
      <ValidationResultBanner
        result={makePassResult({
          warnings: [],
          checks: [
            {
              name: "identity_node_advisory",
              passed: true,
              detail:
                "Node 'passthrough_1' is an identity-shaped passthrough between 'scrape' and sink 'csv_out'. Consider removing it and wiring 'scrape'.on_success directly to 'csv_out'.",
              affected_nodes: ["passthrough_1"],
              outcome_code: null,
            },
          ],
        })}
      />,
    );

    expect(screen.getByText(/Consider removing it/)).toBeInTheDocument();
    expect(
      screen.queryByRole("button", { name: /collapse validation details/i }),
    ).toBeNull();
  });

  it("auto-expands a failed completion advisory check without exposing its compatibility key", () => {
    render(
      <ValidationResultBanner
        result={makePassResult({
          checks: [
            {
              name: "advisor_signoff",
              passed: false,
              detail:
                "The evidence-scoped completion advisory review has not covered this pipeline version.",
              affected_nodes: [],
              outcome_code: null,
            },
          ],
          readiness: {
            authoring_valid: true,
            execution_ready: true,
            completion_ready: false,
            blockers: [
              {
                code: "advisor_signoff_blocked",
                component_id: "pipeline",
                component_type: "pipeline",
                detail:
                  "The evidence-scoped completion advisory review has not covered this pipeline version.",
              },
            ],
          },
        })}
      />,
    );

    expect(
      screen.getByText(/Completion advisory review: The evidence-scoped/),
    ).toBeInTheDocument();
    expect(screen.queryByText(/advisor_signoff:/)).toBeNull();
    expect(
      screen.queryByRole("button", { name: /validation passed\. show details\./i }),
    ).toBeNull();
    expect(
      screen.queryByRole("button", { name: /collapse validation details/i }),
    ).toBeNull();
  });

  it("renders failures expanded with per-component errors, unchanged", () => {
    render(
      <ValidationResultBanner
        result={makePassResult({
          is_valid: false,
          summary: "Validation failed",
          errors: [
            {
              component_id: "select_columns",
              component_type: "transform",
              message: "Bad transform",
              suggestion: "Choose a supported projection.",
            },
          ],
          readiness: {
            ...READY_READINESS,
            authoring_valid: false,
            execution_ready: false,
            completion_ready: false,
          },
        })}
      />,
    );

    expect(screen.getByRole("alert")).toHaveTextContent("Bad transform");
    expect(screen.getByText(/Choose a supported projection/)).toBeInTheDocument();
  });

  // elspeth-7bcc3d5233. The suggestion used to be built INTO the navigable
  // button, so a block <div> sat inside a <button> (invalid flow content), the
  // button's underline ran across the helper note, and the whole four-line
  // block was one hit target. These pin the CONTAINMENT that causes those
  // symptoms — jsdom cannot measure an underline or a list marker, so the
  // mechanism is what is asserted: the suggestion is a sibling of the button
  // inside the same <li>, and only the button navigates.
  it("renders an error suggestion as a sibling of the navigable button, not inside it", async () => {
    const user = userEvent.setup();
    const onComponentClick = vi.fn();
    render(
      <ValidationResultBanner
        result={makePassResult({
          is_valid: false,
          summary: "Validation failed",
          errors: [
            {
              component_id: "select_columns",
              component_type: "transform",
              message: "Bad transform",
              suggestion: "Choose a supported projection.",
            },
          ],
        })}
        componentNames={{ select_columns: "Select columns" }}
        onComponentClick={onComponentClick}
      />,
    );

    const button = screen.getByRole("button", { name: /Bad transform/ });
    const suggestion = screen.getByText(/Choose a supported projection/);

    expect(button).toHaveClass("validation-banner-component-btn--error");
    expect(button.contains(suggestion)).toBe(false);
    expect(button.querySelector("div")).toBeNull();
    expect(suggestion.closest("li")).toBe(button.closest("li"));
    expect(suggestion).toHaveClass("validation-banner-suggestion");

    await user.click(suggestion);
    expect(onComponentClick).not.toHaveBeenCalled();

    await user.click(button);
    expect(onComponentClick).toHaveBeenCalledExactlyOnceWith("select_columns");
  });

  it("renders a warning suggestion as a sibling of the navigable button, not inside it", async () => {
    const user = userEvent.setup();
    const onComponentClick = vi.fn();
    render(
      <ValidationResultBanner
        result={makePassResult({
          warnings: [
            {
              component_id: "csv_source",
              component_type: "source",
              message: "Batch size is small",
              suggestion: "Consider increasing batch size",
            },
          ],
        })}
        componentNames={{ csv_source: "CSV source" }}
        onComponentClick={onComponentClick}
      />,
    );

    const button = screen.getByRole("button", { name: /Batch size is small/ });
    const suggestion = screen.getByText(/Consider increasing batch size/);

    expect(button).toHaveClass("validation-banner-component-btn--warning");
    expect(button.contains(suggestion)).toBe(false);
    expect(button.querySelector("div")).toBeNull();
    expect(suggestion.closest("li")).toBe(button.closest("li"));
    expect(suggestion).toHaveClass("validation-banner-suggestion");

    await user.click(suggestion);
    expect(onComponentClick).not.toHaveBeenCalled();

    await user.click(button);
    expect(onComponentClick).toHaveBeenCalledExactlyOnceWith("csv_source");
  });

  it("still renders a suggestion when the component is not navigable", () => {
    render(
      <ValidationResultBanner
        result={makePassResult({
          is_valid: false,
          summary: "Validation failed",
          errors: [
            {
              component_id: "unknown_component",
              component_type: "transform",
              message: "Bad transform",
              suggestion: "Choose a supported projection.",
            },
          ],
        })}
      />,
    );

    expect(screen.queryByRole("button")).toBeNull();
    expect(
      screen.getByText(/Choose a supported projection/),
    ).toHaveClass("validation-banner-suggestion");
  });
});
